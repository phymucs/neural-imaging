#!/usr/bin/env python
# coding: utf-8

# Basic imports
import gc
import os
import numpy as np
import tqdm
from collections import deque, OrderedDict

# Disable unimportant logging and import TF
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
import tensorflow as tf

# Load my TF models
from models import pipelines
from models.forensics import FAN
from models.jpeg import DJPG

# Helper functions
from helpers import coreutils, tf_helpers, validation, loading


@coreutils.logCall
def construct_models(nip_model, patch_size=128, distribution_jpeg=50, distribution_down='pool', loss_metric='L2', jpeg_approx='sin'):
    """
    Setup the TF model of the entire acquisition and distribution workflow.
    :param nip_model: name of the NIP class
    :param patch_size: patch size for manipulation training (raw patch - rgb patches will be 4 times as big)
    :param distribution_jpeg: JPEG quality level in the distribution channel
    :param distribution_down: Sub-sampling method in the distribution channel ('pool' or 'bilin')
    :param loss_metric: NIP loss metric: L2, L1 or SSIM
    """
    # Sanitize inputs
    if patch_size < 32 or patch_size > 512:
        raise ValueError('The patch size ({}) looks incorrect, typical values should be >= 32 and <= 512'.format(patch_size))

    if distribution_jpeg < 1 or distribution_jpeg > 100:
        raise ValueError('Invalid JPEG quality level ({})'.format(distribution_jpeg))

    if not issubclass(getattr(pipelines, nip_model), pipelines.NIPModel):
        supported_nips = [x for x in dir(pipelines) if x != 'NIPModel' and type(getattr(pipelines, x)) is type and issubclass(getattr(pipelines, x), pipelines.NIPModel)]
        raise ValueError('Invalid NIP model ({})! Available NIPs: ({})'.format(nip_model, supported_nips))
        
    if loss_metric not in ['L2', 'L1', 'SSIM']:
        raise ValueError('Invalid loss metric ({})!'.format(loss_metric))
    
    tf.reset_default_graph()
    sess = tf.Session()

    # The pipeline -----------------------------------------------------------------------------------------------------

    model = getattr(pipelines, nip_model)(sess, tf.get_default_graph(), patch_size=patch_size, loss_metric=loss_metric)
    print('NIP network: {}'.format(model.summary()))

    # Several paths for post-processing --------------------------------------------------------------------------------
    with tf.name_scope('distribution'):

        # Sharpen    
        im_shr = tf_helpers.tf_sharpen(model.y, 0, hsv=True)

        # Bilinear resampling
        im_res = tf.image.resize_images(model.y, [tf.shape(model.y)[1] // 2, tf.shape(model.y)[1] // 2])
        im_res = tf.image.resize_images(im_res, [tf.shape(model.y)[1], tf.shape(model.y)[1]])

        # Gaussian filter
        im_gauss = tf_helpers.tf_gaussian(model.y, 5, 4)

        # Mild JPEG
        tf_jpg = DJPG(sess, tf.get_default_graph(), model.y, None, quality=80, rounding_approximation=jpeg_approx)
        im_jpg = tf_jpg.y

        # Setup operations for detection
        operations = (model.y, im_shr, im_gauss, im_jpg, im_res)
        forensics_classes = ['native', 'sharpen', 'gaussian', 'jpg', 'resample']

        n_classes = len(operations)

        # Concatenate outputs from multiple post-processing paths ------------------------------------------------------
        y_concat = tf.concat(operations, axis=0)

        # Add sub-sampling and JPEG compression in the channel ---------------------------------------------------------
        if distribution_down == 'pool':
            imb_down = tf.nn.avg_pool(y_concat, [1, 2, 2, 1], [1, 2, 2, 1], 'SAME', name='post_downsample')
        elif distribution_down == 'bilin':
            imb_down = tf.image.resize_images(y_concat, [tf.shape(y_concat)[1] // 2, tf.shape(y_concat)[1] // 2])
        else:
            raise ValueError('Unsupported channel down-sampling {}'.format(distribution_down))
            
        jpg = DJPG(sess, tf.get_default_graph(), imb_down, model.x, quality=distribution_jpeg, rounding_approximation=jpeg_approx)
        imb_out = jpg.y

    # Add manipulation detection
    fan = FAN(sess, tf.get_default_graph(), n_classes=n_classes, x=imb_out, nip_input=model.x, n_convolutions=4)
    print('Forensics network parameters: {:,}'.format(fan.count_parameters()))

    # Setup a combined loss and training op
    with tf.name_scope('combined_optimization') as scope:
        nip_fw = tf.placeholder(tf.float32, name='nip_weight')
        lr = tf.placeholder(tf.float32, name='learning_rate')
        loss = fan.loss + nip_fw * model.loss
        adam = tf.train.AdamOptimizer(learning_rate=lr, name='adam')
        opt = adam.minimize(loss, name='opt_combined')
    
    # Initialize all variables
    sess.run(tf.global_variables_initializer())
    
    tf_ops = {
        'sess': sess,
        'nip': model,
        'fan': fan,
        'loss': loss,
        'opt': opt,
        'lr': lr,
        'lambda': nip_fw,
        'operations': operations,
    }
        
    distribution = {    
        'forensics_classes': forensics_classes,
        'channel_jpeg_quality': distribution_jpeg,
        'channel_downsampling': distribution_down,
        'jpeg_approximation': jpeg_approx
    }
    
    return tf_ops, distribution
#     return sess, model, fan, {'loss': loss, 'opt': opt, 'lr': lr, 'lambda': nip_fw}, operations, {'channel_jpeg_quality': distribution_jpeg, 'forensics_classes': forensics_classes, 'downsampling': distribution_down}

# @coreutils.logCall
def train_manipulation_nip(tf_ops, training, distribution, data, directories=None):
    """
    Jointly train the NIP and the FAN models. Training progress and TF checkpoints are saved periodically to the specified directories.
    
    Input parameters:
    
    All input parameters are dictionaries with the following keys:
    
    :param tf_ops: {
        sess     - TF session
        nip      - NIP instance
        fan      - FAN instance
        loss     - TF operation for the loss function
        opt      - TF operation for the optimization step
        lr       - TF placeholder for the learning rate
        lambda   - TF placeholder for the regularization strength
    }
    
    :param training: {
        camera_name           - name of the camera
        use_pretrained_nip    - boolean flag to enable/disable loading a pre-trained model
        nip_weight            - regularization strength to control the trade-off between objectives
        run_number            - number of the current run ()
        n_epochs              - number of training epochs
        learning_rate         - value of the learning rate
    }
    
    :param distribution: {
      channel_jpeg_quality    - JPEG quality level in the distribution channel
      jpeg_approximation      - JPEG approximation mode
      forensics_classes       - names of classes for the FAN to distinguish
      channel_downsampling    - sub-sampling mode in the channel  
    }
            
    :param data: {
        training: {
            x: (N,h,w,4)      - input RAW images (patches will be sampled while training)
            y: (N,2h,2w,3)    - corresponding developed RGB images (patches will be sampled while training)
        }  
        validation: {
            x: (N,p,p,4)      - input RAW patches (for validation)
            y: (N,2p,2p,3)    - corresponding developed RGB patches (for validation)
        }
    }
    
    :param directories: {
        root             - the root output directory for storing training progress and model snapshots 
                           (default: './data/raw/train_manipulation/')
        nip_snapshots    - root directory with pre-trained NIP models 
                           (default: './data/raw/nip_model_snapshots/')
    }
    
    """
    
    # Apply default settings
    directories_def = {'root': './data/raw/train_manipulation/', 'nip_snapshots': './data/raw/nip_model_snapshots/'}
    if directories is not None: 
        directories_def.update(directories)    
    directories = directories_def
    
    # Check if all necessary keys are present
    if any([x not in tf_ops for x in ['sess', 'nip', 'fan', 'loss', 'opt', 'lr', 'lambda']]):
        raise RuntimeError('Missing keys in the tf_ops dictionary! {}'.format(tf_ops.keys()))
        
    if any([x not in training for x in ['camera_name', 'use_pretrained_nip', 'nip_weight', 'run_number', 'n_epochs', 'learning_rate']]):
        raise RuntimeError('Missing keys in the training dictionary! {}'.format(training.keys()))

    if any([x not in distribution for x in ['channel_jpeg_quality', 'jpeg_approximation', 'forensics_classes', 'channel_downsampling']]):
        raise RuntimeError('Missing keys in the distribution dictionary! {}'.format(distribution.keys()))
        
    if any([x not in data for x in ['training', 'validation']]):
        raise RuntimeError('Missing keys in the data dictionary! {}'.format(data.keys()))
        
    if any([x not in data['training'] for x in 'xy']) or any([x not in data['validation'] for x in 'xy']):
        raise RuntimeError('Missing x or y data in the data dictionary! {}'.format({k: v.keys() for k, v in data.items()}))
                    
    print('\n## Training NIP/FAN for manipulation detection: cam={} / lr={:.4f} / run={:3d} / epochs={}, root={}'.format(training['camera_name'], training['nip_weight'], training['run_number'], training['n_epochs'], directories['root']), flush=True)

    nip_save_dir = os.path.join(directories['root'], training['camera_name'], '{nip-model}', 'lr-{:0.4f}'.format(training['nip_weight']), '{:03d}'.format(training['run_number']))
    print('(progress) ->', nip_save_dir)

    model_directory = os.path.join(nip_save_dir.replace('{nip-model}', type(tf_ops['nip']).__name__), 'models')
    print('(model) ---->', model_directory)

    # Enable joint optimization if NIP weight is non-zero
    joint_optimization = training['nip_weight'] != 0

    # Basic setup
    problem_description = 'manipulation detection'
    patch_size = 128
    batch_size = 20
    sampling_rate = 50

    learning_rate_decay_schedule = 100
    learning_rate_decay_rate = 0.90

    # Setup the arrays for storing the current batch - randomly sampled from full-resolution images
    H, W = data['training']['x'].shape[1:3]
    learning_rate = training['learning_rate']

    batch_x = np.zeros((batch_size, patch_size, patch_size, 4), dtype=np.float32)
    batch_y = np.zeros((batch_size, 2 * patch_size, 2 * patch_size, 3), dtype=np.float32)

    # Initialize models
    tf_ops['fan'].init()
    tf_ops['nip'].init()
    tf_ops['sess'].run(tf.global_variables_initializer())

    if training['use_pretrained_nip']:
        tf_ops['nip'].load_model(camera_name=training['camera_name'], out_directory_root=directories['nip_snapshots'])

    n_batches = data['training']['x'].shape[0] // batch_size

    model_list = ['nip', 'fan']

    # Containers for storing loss progression
    loss_epoch = {key: deque(maxlen=n_batches) for key in model_list}
    loss_epoch['similarity-loss'] = deque(maxlen=n_batches)
    loss_last_k_epochs = {key: deque(maxlen=10) for key in model_list}
    loss_last_k_epochs['similarity-loss'] = deque(maxlen=n_batches)
    
    # Create a function which generates labels for each batch
    def batch_labels(batch_size, n_classes):
        return np.concatenate([x * np.ones((batch_size,), dtype=np.int32) for x in range(n_classes)])

    n_classes = len(distribution['forensics_classes'])
    batch_l = batch_labels(batch_size, n_classes)

    # Collect memory usage (seems to be leaking in matplotlib)
    memory = {'tf-ram': [], 'tf-vars': [], 'cpu-proc': [], 'cpu-resource': [] }

    # Collect and print training summary
    training_summary = OrderedDict()
    training_summary['Problem'] = '{}'.format(problem_description)
    training_summary['Classes'] = '{}'.format(distribution['forensics_classes'])
    training_summary['Channel sub-sampling'] = '{}'.format(distribution['channel_downsampling'])
    training_summary['Channel JPEG Quality'] = '{}'.format(distribution['channel_jpeg_quality'])
    training_summary['Channel JPEG Mode'] = '{}'.format(distribution['jpeg_approximation'])
    training_summary['Camera name'] = '{}'.format(training['camera_name'])
    training_summary['Joint optimization'] = '{}'.format(joint_optimization)
    training_summary['NIP Regularization'] = '{}'.format(training['nip_weight'])
    training_summary['FAN model'] = '{}'.format(tf_ops['fan'].summary())
    training_summary['NIP model'] = '{}'.format(tf_ops['nip'].summary())
    training_summary['NIP loss'] = '{}'.format(tf_ops['nip'].loss_metric)
    training_summary['Use pre-trained NIP'] = '{}'.format(training['use_pretrained_nip'])
    training_summary['# Epochs'] = '{}'.format(training['n_epochs'])
    training_summary['Patch size'] = '{}'.format(patch_size)
    training_summary['Batch size'] = '{}'.format(batch_size)
    training_summary['Learning rate'] = '{}'.format(training['learning_rate'])
    training_summary['Learning rate decay schedule'] = '{}'.format(learning_rate_decay_schedule)
    training_summary['Learning rate decay rate'] = '{}'.format(learning_rate_decay_rate)
    training_summary['# train. images'] = '{}'.format(data['training']['x'].shape)
    training_summary['# valid. images'] = '{}'.format(data['validation']['x'].shape)
    training_summary['# batches'] = '{}'.format(batch_x.shape)
    training_summary['NIP input patch'] = '{}'.format(tf_ops['nip'].x.shape)
    training_summary['NIP output patch'] = '{}'.format(tf_ops['nip'].y.shape)
    training_summary['FAN input patch'] = '{}'.format(tf_ops['fan'].x.shape)
    training_summary['memory_consumption'] = memory

    print('\n')
    for k, v in training_summary.items():
        print('{:30s}: {}'.format(k, v))
    print('\n', flush=True)

    with tqdm.tqdm(total=training['n_epochs'], ncols=120, desc='Train') as pbar:
        
        epoch = 0
        conf = np.identity(len(distribution['forensics_classes']))

        for epoch in range(0, training['n_epochs']):

            # Fill the batch with random crops of the images
            for batch_id in range(n_batches):

                # Extract random patches for the current batch of images
                batch_x = data.next_training_batch(batch_id, batch_size, patch_size)
#                 for b in range(batch_size):
#                     xx = np.random.randint(0, W - patch_size)
#                     yy = np.random.randint(0, H - patch_size)
#                     batch_x[b, :, :, :] = data['training']['x'][batch_id * batch_size + b, yy:yy + patch_size, xx:xx + patch_size, :].astype(np.float) / (2**16 - 1)
#                     batch_y[b, :, :, :] = data['training']['y'][batch_id * batch_size + b, (2*yy):(2*yy + 2*patch_size), (2*xx):(2*xx + 2*patch_size), :].astype(np.float) / (2**8 - 1)

                if joint_optimization:
                    # Make custom optimization step                    
                    comb_loss, nip_loss, _ = tf_ops['sess'].run([tf_ops['loss'], tf_ops['nip'].loss, tf_ops['opt']], feed_dict={
                        tf_ops['nip'].x: batch_x,
                        tf_ops['nip'].y_gt: batch_y,
                        tf_ops['fan'].y: batch_l,
                        tf_ops['lr']: learning_rate,
                        tf_ops['lambda']: training['nip_weight']                        
                    })                    
                    
                    loss_epoch['nip'].append(nip_loss)
                else:
                    # Update only the forensics network
                    comb_loss = tf_ops['fan'].training_step(batch_x, batch_l, learning_rate)
                    nip_loss = np.nan

                loss_epoch['fan'].append(comb_loss)
                loss_epoch['nip'].append(nip_loss)

            # Average and record loss values
            for model_name in model_list:
                tf_ops[model_name].train_perf['loss'].append(float(np.mean(loss_epoch[model_name])))
                loss_last_k_epochs[model_name].append(tf_ops[model_name].train_perf['loss'][-1])

            if epoch % sampling_rate == 0:

                # Validate the NIP model
                if joint_optimization:
                    values = validation.validate_nip(tf_ops['nip'], data['validation']['x'][::50], data['validation']['y'][::50], None, epoch=epoch, show_ref=True, loss_type=tf_ops['nip'].loss_metric)
                    for metric, val_array in zip(['ssim', 'psnr', 'loss'], values):
                        tf_ops['nip'].valid_perf[metric].append(float(np.mean(val_array)))

                # Validate the forensics network
                accuracy = validation.validate_fan(tf_ops['fan'], data['validation']['x'][::1], lambda x: batch_labels(x, n_classes), n_classes)
                tf_ops['fan'].valid_perf['accuracy'].append(accuracy)

                # Confusion matrix
                conf = validation.confusion(tf_ops['fan'], data['validation']['x'], lambda x: batch_labels(x, n_classes))

                # Visualize current progress
                # TODO Memory is leaking here - looks like some problem in matplotlib - skip for now
                # validation.visualize_manipulation_training(model, fan, conf, epoch, nip_save_dir, classes=distribution['forensics_classes'])

                # Save progress stats
                validation.save_training_progress(training_summary, tf_ops['nip'], tf_ops['fan'], conf, nip_save_dir)

                # Monitor memory usage
                # gc.collect()
                memory['tf-ram'].append(round(tf_helpers.memory_usage_tf(tf_ops['sess']) / 1024 / 1024, 1))
                memory['tf-vars'].append(round(tf_helpers.memory_usage_tf_variables() / 1024 / 1024, 1))
                memory['cpu-proc'].append(round(coreutils.memory_usage_proc(), 1))
                memory['cpu-resource'].append(round(coreutils.memory_usage_resource(), 1))

            if epoch % learning_rate_decay_schedule == 0:
                learning_rate *= learning_rate_decay_rate

            pbar.set_postfix(nip=np.log10(np.mean(loss_last_k_epochs['nip'])).round(1),
                             fan=np.mean(loss_last_k_epochs['fan']),
                             acc=tf_ops['fan'].valid_perf['accuracy'][-1],
                             psnr=tf_ops['nip'].valid_perf['psnr'][-1] if len(tf_ops['nip'].valid_perf['psnr']) > 0 else np.nan,
                             ram=round(memory['cpu-proc'][-1]//1024, 2))
            pbar.update(1)

    # Plot final results
    values = validation.validate_nip(tf_ops['nip'], data['validation']['x'][::50], data['validation']['y'][::50], nip_save_dir, epoch=epoch, show_ref=True, loss_type='L2')
    for metric, val_array in zip(['ssim', 'psnr', 'loss'], values):
        tf_ops['nip'].valid_perf[metric].append(float(np.mean(val_array)))

    # Save model progress
    validation.save_training_progress(training_summary, tf_ops['nip'], tf_ops['fan'], conf, nip_save_dir)

    # Visualize current progress
    validation.visualize_manipulation_training(tf_ops['nip'], tf_ops['fan'], conf, epoch, nip_save_dir, classes=distribution['forensics_classes'])

    # Save models
    # Root     : train_manipulation / camera_name / {INet} / lr-01 / 001 / models / {INet/FAN}
    print('Saving models...')
    tf_ops['nip'].save_model(training['camera_name'], os.path.join(model_directory, '{nip-model}'), epoch)
    tf_ops['fan'].save_model(os.path.join(model_directory, 'FAN'), epoch)
    
    return nip_save_dir.replace('{nip-model}', type(tf_ops['nip']).__name__)
