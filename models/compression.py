import numpy as np
import tensorflow as tf

from models.tfmodel import TFModel
from helpers import tf_helpers, paramspec


class DCN(TFModel):
    """
    An abstract class for deriving image compression models.

    # Attributes set-up by the abstract class:
      x                    - model input
      patch_size           - patch size
      latent_bpf           - number of bits per feature of the latent representation
      train_codebook       - whether the codebook
      codebook             - the quantization code book (TF)
      entropy_weight       - entropy regularization strength for model loss
      default_val_is_train - used to set default value for the 'is_training' flag during model inference
                             (useful for models with batch normalization)
      scale_latent         - bool flag indicating scaling of the latent representation
      use_batchnorm        - bool flag indicating the use of batch norm in the model

      weights              - soft quantization weights (TF)
      histogram            - latent space histogram based on soft quantization (TF)
      entropy              - entropy estimation (TF)

    # Attributes set-up by the derived classes:
      y
      latent_pre           - latent representation before quantization
      latent_post          - latent representation after quantization
      latent_shape         - shape of the latent tensor (before flattening)
      n_latent             - dimensionality of the latent representation
      _h                   - hyper parameters
    """

    def __init__(self, sess, graph, label=None, x=None, nip_input=None, patch_size=128, latent_bpf=4, train_codebook=False, entropy_weight=None, default_val_is_train=True, scale_latent=False, use_batchnorm=False, use_gdn=False, **kwargs):
        """
        Creates a forensic analysis network.

        :param sess: TF session or None (creates a new one)
        :param graph: TF graph or None (creates a new one)
        :param label: a suffix for the name scope of the model
        """
        super().__init__(sess, graph, label)

        # Basic value sanitization

        if latent_bpf < 1 or latent_bpf > 8:
            raise ValueError('Invalid value for latent_bpf! Valid range: 1 - 8')

        if entropy_weight is not None and entropy_weight < 0:
            raise ValueError('Invalid value for entropy_weight! Valid range: >=0')

        self.verbose = True
        self.patch_size = patch_size
        self.nip_input = nip_input
        self.latent_bpf = latent_bpf
        self.train_codebook = train_codebook
        self.entropy_weight = entropy_weight
        self.default_val_is_train = default_val_is_train
        self.scale_latent = scale_latent
        self.use_batchnorm = use_batchnorm
        self.use_gdn = use_gdn

        # Remember parameters passed to the constructor
        # self.args = kwargs

        with self.graph.as_default():
            
            # Setup inputs:
            # - if possible take external tensor as input, otherwise create a placeholder
            # - if external input is given (from a NIP model), remember the input to the NIP model to facilitate 
            #   convenient operation of the class (see helper methods 'process*')
            if x is None:
                x = tf.placeholder(tf.float32, shape=(None, patch_size, patch_size, 3), name='x_{}'.format(self.scoped_name))
                self.use_nip_input = False
            else:
                self.use_nip_input = True
            
            self.x = x
            
            # Setup quantization code book -----------------------------------------------------------------------------
            with tf.name_scope('{}/optimization'.format(self.scoped_name)):
                
                with tf.name_scope('entropy'):
                                        
                    # Initialize the quantization codebook
                    qmin = -2 ** (self.latent_bpf - 1) + 1
                    qmax = 2 ** (self.latent_bpf - 1)
                                        
                    self.log('Initializing {} codebook ({} bpf): from {} to {}'.format(
                        'trainable' if self.train_codebook else 'fixed', self.latent_bpf, qmin, qmax))

                    if self.train_codebook:
                        bin_centers = tf.get_variable('{}/quantization/codebook'.format(self.scoped_name),
                            initializer=tf.constant_initializer(np.arange(qmin, qmax + 1)),
                            shape=(1, 2 ** self.latent_bpf))
                    else:
                        bin_centers = tf.constant(np.arange(qmin, qmax + 1), shape=(1, 2 ** self.latent_bpf), dtype=tf.float32)                        
                    self.codebook = bin_centers                
            
            # Construct the actual model -------------------------------------------------------------------------------
            self.construct_model(kwargs)

            # Check if the model has set all expected attributes
            setup_status = {key: hasattr(self, key) for key in ['y', 'latent_pre', 'latent_post', 'latent_shape', 'n_latent']}
            if not all(setup_status.values()):
                raise NotImplementedError('The model construction function has failed to set-up some attributes: {}'.format([key for key, value in setup_status.items() if not value]))

            # Add entropy estimation and model optimization operations -------------------------------------------------
            with tf.name_scope('{}/optimization'.format(self.scoped_name)):

                # Estimate entropy of the latent representation
                with tf.name_scope('entropy'):

                    soft_quantization_sigma = 5
                    prec_dtype = tf.float64
                    v = 25
                    eps = 1e-72

                    values = tf.reshape(self.latent_pre, (-1, 1))
                    
                    assert(self.codebook.shape[0] == 1)
                    assert(self.codebook.shape[1] > 1)                    
                    
                    # Compute soft-quantization
                    if v <= 0:
                        self.log('Entropy estimation using Gaussian soft quantization')
                        dff = tf.cast(values, dtype=prec_dtype) - tf.cast(self.codebook, dtype=prec_dtype)
                        weights = tf.exp(-soft_quantization_sigma * tf.pow(dff, 2))
                    else:
                        # t-Student-like distance measure with heavy tails
                        self.log('Entropy estimation using t-Student soft quantization')
                        dff = tf.cast(values, dtype=prec_dtype) - tf.cast(self.codebook, dtype=prec_dtype)
                        dff = soft_quantization_sigma * dff
                        weights = tf.pow((1 + tf.pow(dff, 2)/v), -(v+1)/2)

                    weights = (weights + eps) / (eps + tf.reduce_sum(weights, axis=1, keepdims=True))
                    
                    assert(weights.shape[1] == np.prod(self.codebook.shape))
                    
                    # Compute soft histogram
                    histogram = tf.reduce_mean(weights, axis=0)
                    histogram = tf.clip_by_value(histogram, 1e-9, tf.float32.max)
#                     histogram = tf.maximum(histogram, 1e-9)
                    histogram = histogram / tf.reduce_sum(histogram)

                    self.entropy = - tf.reduce_sum(histogram * tf.log(histogram)) / 0.6931  # 0.6931 - log(2)
                    self.histogram = histogram
                    self.weights = weights
                
                # Loss and SSIM
                self.ssim = tf.reduce_mean(tf.image.ssim(self.x, tf.clip_by_value(self.y, 0, 1), max_val=1))
                self.loss = tf.nn.l2_loss(self.x - self.y)
                if self.entropy_weight is not None:
                    self.loss = self.loss + self.entropy_weight * self.entropy
                self.log('Initializing loss: L2 {}'.format('+ {:.2f} * entropy'.format(self.entropy_weight) if self.entropy_weight is not None else ''))
                
                # Optimization
                update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
                with tf.control_dependencies(update_ops):
                    self.lr = tf.placeholder(tf.float32, name='{}_learning_rate'.format(self.scoped_name))
                    self.adam = tf.train.AdamOptimizer(learning_rate=self.lr)
                    self.opt = self.adam.minimize(self.loss, var_list=self.parameters)

    def log(self, message):
        if self.verbose:
            print(' ', message)

    def construct_model(self, params):
        raise NotImplementedError('Not implemented!')
        
    def reset_performance_stats(self):
        self.performance = {
            'loss': {'training': [], 'validation': []},
            'entropy': {'training': [], 'validation': []},
            'ssim': {'training': [], 'validation': []}
        }

    def get_tf_histogram(self, batch_x, is_training=None):
        with self.graph.as_default():
            feed_dict = {
                self.x if not self.use_nip_input else self.nip_input: batch_x,
            }

            if hasattr(self, 'is_training'):
                feed_dict[self.is_training] = is_training if is_training is not None else self.default_val_is_train

            return self.sess.run(self.histogram, feed_dict=feed_dict)

    def compress(self, batch_x, is_training=None):
        with self.graph.as_default():
            
            feed_dict = {
                self.x if not self.use_nip_input else self.nip_input: batch_x,
            }            
            
            if hasattr(self, 'is_training'):
                feed_dict[self.is_training] = is_training if is_training is not None else self.default_val_is_train

            y = self.sess.run(self.latent_post, feed_dict=feed_dict)
            return y

    def compress_soft(self, batch_x, is_training=None):
        with self.graph.as_default():
            
            feed_dict = {
                self.x if not self.use_nip_input else self.nip_input: batch_x,
            }
            
            if hasattr(self, 'is_training'):
                feed_dict[self.is_training] = is_training if is_training is not None else self.default_val_is_train
            
            y = self.sess.run(self.latent_pre, feed_dict=feed_dict)
            return y        
        
    def decompress(self, batch_z):
        with self.graph.as_default():
            
            feed_dict = {
                self.latent_post: batch_z
            }
            if hasattr(self, 'dropout'):
                feed_dict[self.dropout] = 1.0
                
            y = self.sess.run(self.y, feed_dict)
            return y.clip(0, 1)
            
    def process(self, batch_x, dropout_keep_prob=1.0, is_training=None):
        """

        """
        with self.graph.as_default():
            
            feed_dict={
                self.x if not self.use_nip_input else self.nip_input: batch_x
            }
            
            if hasattr(self, 'dropout'):
                feed_dict[self.dropout] = dropout_keep_prob
                
            if hasattr(self, 'is_training'):
                feed_dict[self.is_training] = is_training if is_training is not None else self.default_val_is_train
              
            y = self.sess.run(self.y, feed_dict)
            return y.clip(0, 1)
    
    def process_direct(self, batch_x, dropout_keep_prob=1.0, is_training=None):
        """
        Returns the predicted class for an image batch. The input is always fed to the FAN model directly.
        """
        with self.graph.as_default():
            feed_dict = {
                self.x: batch_x
            }
            if hasattr(self, 'dropout'):
                feed_dict[self.dropout] = dropout_keep_prob
                
            if hasattr(self, 'is_training'):
                feed_dict[self.is_training] = is_training if is_training is not None else self.default_val_is_train
                
            y = self.sess.run(self.y, feed_dict)
            return y.clip(0, 1)
    
    def training_step(self, batch_x, learning_rate, dropout_keep_prob=1.0):
        """
        Make a single training step and return current loss. Only the FAN model is updated.
        """
        with self.graph.as_default():
            feed_dict = {
                    self.x if not self.use_nip_input else self.nip_input: batch_x,
                    self.lr: learning_rate
            }
            if hasattr(self, 'dropout'):
                feed_dict[self.dropout] = dropout_keep_prob
                
            if hasattr(self, 'is_training'):
                feed_dict[self.is_training] = True                
            
            _, loss, ssim, entropy = self.sess.run([self.opt, self.loss, self.ssim, self.entropy], feed_dict)
            return {
                'loss': np.sqrt(2 * loss),  # The L2 loss in TF is computed differently (half of non-square rooted norm)
                'ssim': ssim,
                'entropy': entropy
            }

    def compression_stats(self, patch_size=None, n_latent_bytes=None):

        n_latent_bytes = n_latent_bytes or self.latent_bpf / 8

        ps = patch_size or self.patch_size        
        if ps is None:
            raise ValueError('Patch size not specified!')
            
        bitmap_size = ps * ps * 3
        return {
            'rate': bitmap_size / (n_latent_bytes * self.n_latent),
            'bpp': 8 * self.n_latent * n_latent_bytes / (ps * ps),
            'bpf': 8 * n_latent_bytes,
            'bytes': self.n_latent * n_latent_bytes
        }
    
    def summary(self):
        return 'dcn with {}-D latent representation [{:,} parameters]'.format(self.n_latent, self.count_parameters())
    
    @property
    def model_code(self):
        if not hasattr(self, 'n_latent'):
            raise ValueError('The model does not report the latent space dimensionality.')
        
        return '{}-{}D'.format(type(self).__name__, self.n_latent)        

    def get_parameters(self):
        return {
            'latent_bpf': self.latent_bpf,
            'train_codebook': self.train_codebook,
            'entropy_weight': self.entropy_weight,
            'default_val_is_train': self.default_val_is_train,
            'scale_latent': self.scale_latent,
            'use_batchnorm': self.use_batchnorm,
            'use_gdn': self.use_gdn
        }

    def get_codebook(self, bpf=None, lloyd=False):
        if hasattr(self, '_h') and hasattr(self._h, 'rounding'):

            bpf = bpf or self.latent_bpf

            if self._h.rounding in {'soft', 'identity'}:
                qmin = -2 ** (bpf - 1) + 1
                qmax = 2 ** (bpf - 1)
                return np.arange(qmin, qmax + 1).reshape((-1,))
            else:
                return self.sess.run(self.codebook).reshape((-1,))
        else:
            return self.sess.run(self.codebook).reshape((-1,))

    
class AutoencoderDCN(DCN):

    def construct_model(self, params):

        # Define expected hyper parameters and their values
        self._h = paramspec.ParamSpec({
            'n_filters': (8, int, (2, None)),
            'n_fscale': (2.0, float, (0.25, 4)),
            'n_latent': (0, int, (-1, None)),
            'kernel': (5, int, {3, 5, 7, 9, 11}),
            'n_layers': (3, int, (1, np.log2(self.patch_size) if self.patch_size is not None else 10)),  # Ensure valid latent representation
            'res_layers': (0, int, (0, 3)),
            'dropout': (False, bool, None),
            'rounding': ('soft', str, {'identity', 'soft', 'soft-codebook', 'sin'}),
            'activation': ('leaky_relu', str, set(tf_helpers.activation_mapping.keys()))
        })

        self._h.update(**params)
        self.uses_bottleneck = self._h.n_latent > 0

        net = self.x
        self.log('input size: {}'.format(net.shape))

        # Encoder ------------------------------------------------------------------------------------------------------

        latent_activation = None
        last_activation = None
        activation = tf_helpers.activation_mapping[self._h.activation]
        current_n_filters = self._h.n_filters

        # Add convolutional layers
        for r in range(self._h.n_layers):

            current_activation = activation \
                if (self._h.n_latent > 0 or (self._h.n_latent == 0 and r < self._h.n_layers - 1)) \
                else latent_activation
            net = tf.contrib.layers.conv2d(net, current_n_filters, self._h.kernel,
                                           stride=2, activation_fn=current_activation,
                                           scope='{}/encoder/conv_{}'.format(self.scoped_name, r))

            self.log('conv size: {} + {}'.format(net.shape, current_activation.__name__ if current_activation is not None else None))

            if r != self._h.n_layers - 1:
                current_n_filters *= self._h.n_fscale

        # Add residual blocks
        for r in range(self._h.res_layers):
            res_input = tf.nn.leaky_relu(net, name='{}/encoder/res_{}/lrelu'.format(self.scoped_name, r))
            resnet = tf.contrib.layers.conv2d(res_input, current_n_filters, 3, stride=1, activation_fn=activation,
                                              scope='{}/encoder/res_{}/conv_{}'.format(self.scoped_name, r, 0))
            resnet = tf.contrib.layers.conv2d(resnet, current_n_filters, 3, stride=1, activation_fn=None,
                                              scope='{}/encoder/res_{}/conv_{}'.format(self.scoped_name, r, 1))
            net = tf.add(net, resnet, name='{}/encoder/res_{}/sum'.format(self.scoped_name, r))
            self.log('residual block: {}'.format(net.shape))

        # Latent representation ----------------------------------------------------------------------------------------

        # Compute the shape of the latent representation
        z_spatial = int(self.patch_size / (2**self._h.n_layers))
        z_features = int(self._h.n_filters * (self._h.n_fscale**(self._h.n_layers-1)))
        self.latent_shape = [-1, z_spatial, z_spatial, z_features]
        assert z_spatial > 0, 'Invalid size of the latent representation!'

        # If a smaller linear bottleneck is specified explicitly - add dense layers to make the projection
        if self._h.n_latent is not None and self._h.n_latent != 0:
            flat = tf.contrib.layers.flatten(net, scope='{}/encoder/flatten_{}'.format(self.scoped_name, 0))
            self.log('flatten size: {}'.format(flat.shape))

            if self._h.n_latent > 0:
                flat = tf.contrib.layers.fully_connected(flat, self._h.n_latent, activation_fn=latent_activation,
                                                         scope='{}/encoder/dense_{}'.format(self.scoped_name, 0))
                latent = tf.identity(flat, name='{}/encoder/latent_raw'.format(self.scoped_name))
                self.log('dense size: {}'.format(flat.shape))
            else:
                latent = tf.identity(flat, name='{}/encoder/latent_raw'.format(self.scoped_name))
        else:
            latent = tf.identity(net, name='{}/encoder/latent_raw'.format(self.scoped_name))

        # Use GDN to Gaussianize data
        if self.use_gdn:
            latent = tf.contrib.layers.GDN(latent)
            self.log('GDN: {}'.format(latent.shape))

        # Add batch norm to normalize the latent representation
        if self.use_batchnorm:
            self.pre_bn = latent  # TODO Temporarily added for debugging
            self.is_training = tf.placeholder(tf.bool, shape=(), name='{}/is_training'.format(self.scoped_name))
            latent = tf.contrib.layers.batch_norm(latent, scale=False, is_training=self.is_training,
                                                  scope='{}/encoder/bn_{}'.format(self.scoped_name, 0))
            self.log('batch norm: {}'.format(latent.shape))

        # Learn a scaling factor for the latent features to encourage greater values (facilitates quantization)
        if self.scale_latent:
            # scaling_factor = np.max((1, np.power(2, self.latent_bpf - 2)))
            scaling_factor = 1
            alphas = tf.get_variable('{}/encoder/latent_scaling'.format(self.scoped_name), shape=(), dtype=tf.float32,
                                     initializer=tf.constant_initializer(scaling_factor))
            latent = tf.multiply(alphas, latent, name='{}/encoder/latent_scaled'.format(self.scoped_name))
            self.log('scaling latent representation - init:{}'.format(scaling_factor))

        # Add identity to facilitate better display in the TF graph
        latent = tf.identity(latent, name='{}/latent'.format(self.scoped_name))
        self.n_latent = int(np.prod(latent.shape[1:]))

        # Quantize the latent representation and remember tensors before and after the process
        self.latent_pre = latent
        latent = tf_helpers.quantization(latent, '{}/quantization'.format(self.scoped_name), 'latent_quantized',
                                         self._h.rounding, codebook_tensor=self.codebook)
        self.log('quantization with {} rounding'.format(self._h.rounding))
        self.latent_post = latent
        self.log('latent size: {} + quant:{}'.format(latent.shape, self._h.rounding))

        # If using a bottleneck layer, inverse the projection
        if self._h.n_latent > 0:
            inet = tf.contrib.layers.fully_connected(latent, int(np.prod(self.latent_shape[1:])),
                                                     scope='{}/decoder/dense_{}'.format(self.scoped_name, 0),
                                                     activation_fn=activation)
            self.log('dense size: {} + {}'.format(inet.shape, activation))
        else:
            inet = latent

        # Add dropout
        if self._h.dropout:

            if not hasattr(self, 'is_training'):
                self.is_training = tf.placeholder(tf.bool, shape=(), name='{}/is_training'.format(self.scoped_name))

            self.dropout = tf.placeholder(tf.float32, name='{}/droprate'.format(self.scoped_name), shape=())
            inet = tf.contrib.layers.dropout(inet, keep_prob=self.dropout, is_training=self.is_training,
                                             scope='{}/dropout'.format(self.scoped_name))
            self.log('dropout size: {}'.format(net.shape))

        # Decoder ------------------------------------------------------------------------------------------------------

        # Just in case - make sure we have a multidimensional tensor before we start the convolutions
        inet = tf.reshape(inet, self.latent_shape, name='{}/decoder/reshape_{}'.format(self.scoped_name, 0))
        self.log('reshape size: {}'.format(inet.shape))

        # Add residual blocks
        for r in range(self._h.res_layers):
            res_input = tf.nn.leaky_relu(inet, name='{}/encoder/res_{}/lrelu'.format(self.scoped_name, r))
            resnet = tf.contrib.layers.conv2d(res_input, current_n_filters, 3, stride=1,
                                              scope='{}/decoder/res_{}/conv_{}'.format(self.scoped_name, r, 0),
                                              activation_fn=activation)
            resnet = tf.contrib.layers.conv2d(resnet, current_n_filters, 3, stride=1, activation_fn=None,
                                              scope='{}/decoder/res_{}/conv_{}'.format(self.scoped_name, r, 1))
            inet = tf.add(inet, resnet, name='{}/decoder/res_{}/sum'.format(self.scoped_name, r))
            self.log('residual block: {}'.format(net.shape))

        # Up-sampling / transpose convolutions
        for r in range(self._h.n_layers):
            current_activation = last_activation if r == self._h.n_layers - 1 else activation
            inet = tf.contrib.layers.conv2d(inet, 2 * current_n_filters, self._h.kernel, stride=1,
                                            scope='{}/decoder/tconv_{}'.format(self.scoped_name, r),
                                            activation_fn=current_activation)
            self.log('conv size: {} + {}'.format(inet.shape, current_activation.__name__ if current_activation is not None else None))
            inet = tf.depth_to_space(inet, 2, name='{}/decoder/d2s_{}'.format(self.scoped_name, r))
            self.log('d2s size: {} + {}'.format(inet.shape, None))
            current_n_filters = current_n_filters // self._h.n_fscale

        inet = tf.contrib.layers.conv2d(inet, 3, self._h.kernel, stride=1, activation_fn=last_activation,
                                        scope='{}/decoder/tconv_out'.format(self.scoped_name))
        self.log('conv->out size: {} + {}'.format(inet.shape, last_activation))
        y = tf.identity(inet, name='y')

        self.y = y
        self.latent = latent

    @property
    def model_code(self):
        parameter_summary = []

        if hasattr(self, 'latent_shape'):
            parameter_summary.append('x'.join(str(x) for x in self.latent_shape[1:]))

        layer_summary = []
        if 'n_layers' in self._h:
            layer_summary.append('{:d}C'.format(int(self._h.n_layers)))
        if 'res_layers' in self._h and self._h.res_layers > 0:
            layer_summary.append('{:d}R'.format(int(self._h.res_layers)))
        if self.uses_bottleneck:
            layer_summary.append('F')
        if 'dropout' in self._h and self._h.dropout:
            layer_summary.append('+D')
        if hasattr(self, 'use_batchnorm') and self.use_batchnorm:
            layer_summary.append('+BN')
        if hasattr(self, 'use_gdn') and self.use_gdn:
            layer_summary.append('+GDN')

        parameter_summary.append(''.join(layer_summary))
        parameter_summary.append('r:{}'.format(self._h.rounding))
        parameter_summary.append('Q+{}bpf'.format(self.latent_bpf) if self.train_codebook else 'Q-{}bpf'.format(self.latent_bpf))
        parameter_summary.append('S+' if self.scale_latent else 'S-')
        if self.entropy_weight is not None:
            parameter_summary.append('H+{:.2f}'.format(self.entropy_weight))

        return '{}/{}'.format(super().model_code, '-'.join(parameter_summary))

    def get_parameters(self):
        params = super().get_parameters()
        params.update(self._h.to_json())
        return params


class TwitterDCN(DCN):
    """
    Auto-encoder architecture described in:
    [1] L. Theis, W. Shi, A. Cunningham, and F. Huszár, “Lossy Image Compression with Compressive
    coders,” Mar. 2017.
    """

    def construct_model(self, params):

        # Define expected hyper parameters and their values
        self._h = paramspec.ParamSpec({
            'n_features': (96, int, (4, 128)),
            'rounding': ('soft', str, {'identity', 'soft', 'soft-codebook', 'sin'}),
            'activation': ('leaky_relu', str, set(tf_helpers.activation_mapping.keys()))
        })

        self._h.update(**params)

        activation = tf_helpers.activation_mapping[self._h.activation]
        latent_activation = tf.nn.tanh
        last_activation = tf.nn.sigmoid

        # self.n_layers = 9
        self.latent_shape = (1, self.patch_size // 8, self.patch_size // 8, self._h.n_features)
        self.n_latent = int(np.prod(self.latent_shape))

        self.log('Building Twitter DCN with d-latent={}'.format(self.n_latent))

        with tf.name_scope('{}/encoder/normalization'.format(self.scoped_name)):
            net = 2 * (self.x - 0.5)
            self.log('norm: {}'.format(net.shape))

        # Encoder ---------------------------------------------------------------------------------------------------------

        net = tf.contrib.layers.conv2d(net, 64, 5, stride=2, activation_fn=activation, scope='{}/encoder/conv_{}'.format(self.scoped_name, 0))
        self.log('conv:2 {} + {}'.format(net.shape, self._h.activation))
        net = tf.contrib.layers.conv2d(net, 128, 5, stride=2, activation_fn=None, scope='{}/encoder/conv_{}'.format(self.scoped_name, 1))
        self.log('conv:2 {} + {}'.format(net.shape, self._h.activation))

        resnet = tf.contrib.layers.conv2d(tf.nn.leaky_relu(net, name='{}/encoder/conv_{}/lrelu'.format(self.scoped_name, 1)), 128, 3, stride=1, activation_fn=activation, scope='{}/encoder/conv_{}'.format(self.scoped_name, 2))
        resnet = tf.contrib.layers.conv2d(resnet, 128, 3, stride=1, activation_fn=None, scope='{}/encoder/conv_{}'.format(self.scoped_name, 3))
        net = tf.add(net, resnet, name='{}/encoder/sum_a{}'.format(self.scoped_name, 0))
        self.log('res block {}'.format(net.shape))

        resnet = tf.contrib.layers.conv2d(net, 128, 3, stride=1, activation_fn=activation, scope='{}/encoder/conv_{}'.format(self.scoped_name, 4))
        resnet = tf.contrib.layers.conv2d(resnet, 128, 3, stride=1, activation_fn=None, scope='{}/encoder/conv_{}'.format(self.scoped_name, 5))
        net = tf.add(net, resnet, name='{}/encoder/sum_b{}'.format(self.scoped_name, 1))
        self.log('res block {}'.format(net.shape))

        resnet = tf.contrib.layers.conv2d(net, 128, 3, stride=1, activation_fn=activation, scope='{}/encoder/conv_{}'.format(self.scoped_name, 6))
        resnet = tf.contrib.layers.conv2d(resnet, 128, 3, stride=1, activation_fn=None, scope='{}/encoder/conv_{}'.format(self.scoped_name, 7))
        net = tf.add(net, resnet, name='{}/encoder/sum_c{}'.format(self.scoped_name, 2))
        self.log('res block {}'.format(net.shape))

        net = tf.contrib.layers.conv2d(net, self._h.n_features, 5, stride=2, activation_fn=None, scope='{}/encoder/conv_{}'.format(self.scoped_name, 8))
        self.log('conv:2 {} + {} activation'.format(net.shape, None))

        # Latent space -------------------------------------------------------------------------------------------------

        latent = tf.identity(net, name='{}/encoder/latent_raw'.format(self.scoped_name))

        # Use GDN to Gaussianize data
        if self.use_gdn:
            latent = tf.contrib.layers.GDN(latent)
            self.log('GDN: {}'.format(latent.shape))

        # Add batch norm to normalize the latent representation
        if self.use_batchnorm:
            self.pre_bn = latent  # TODO Temporarily added for debugging
            self.is_training = tf.placeholder(tf.bool, shape=(), name='{}/is_training'.format(self.scoped_name))
            latent = tf.contrib.layers.batch_norm(latent, scale=False, is_training=self.is_training,
                                                  scope='{}/encoder/bn_{}'.format(self.scoped_name, 0))
            self.log('batch norm: {}'.format(latent.shape))

        # Learn a scaling factor for the latent features to encourage greater values (facilitates quantization)
        if self.scale_latent:
            # scaling_factor = np.max((1, np.power(2, self.latent_bpf - 2)))
            scaling_factor = 1
            alphas = tf.get_variable('{}/encoder/latent_scaling'.format(self.scoped_name), shape=(), dtype=tf.float32, initializer=tf.constant_initializer(scaling_factor))
            latent = tf.multiply(alphas, latent, name='{}/encoder/latent_scaled'.format(self.scoped_name))
            self.log('scaling latent representation - init:{}'.format(scaling_factor))

        # Add identity to facilitate better display in the TF graph
        latent = tf.identity(latent, name='{}/latent'.format(self.scoped_name))
        self.n_latent = int(np.prod(latent.shape[1:]))

        # Quantize the latent representation and remember tensors before and after the process
        self.latent_pre = latent
        latent = tf_helpers.quantization(latent, '{}/quantization'.format(self.scoped_name), 'latent_quantized',
                                         self._h.rounding, codebook_tensor=self.codebook)
        self.log('quantization with {} rounding'.format(self._h.rounding))
        self.latent_post = latent
        self.log('latent size: {} + quant:{}'.format(latent.shape, self._h.rounding))

        # Decoder ---------------------------------------------------------------------------------------------------

        inet = tf.contrib.layers.conv2d(latent, 512, 3, stride=1, activation_fn=None, scope='{}/decoder/conv_{}'.format(self.scoped_name, 0))
        self.log('conv:1 {} + {} activation'.format(inet.shape, None))
        inet = tf.depth_to_space(inet, 2, name='{}/decoder/d2s_{}'.format(self.scoped_name, 0))
        self.log('dts {}'.format(inet.shape))

        resnet = tf.contrib.layers.conv2d(inet, 128, 3, stride=1, activation_fn=activation, scope='{}/decoder/conv_{}'.format(self.scoped_name, 1))
        resnet = tf.contrib.layers.conv2d(resnet, 128, 3, stride=1, activation_fn=None, scope='{}/decoder/conv_{}'.format(self.scoped_name, 2))
        inet = tf.add(inet, resnet, name='{}/decoder/sum_a{}'.format(self.scoped_name, 0))
        self.log('res block {}'.format(inet.shape))

        resnet = tf.contrib.layers.conv2d(inet, 128, 3, stride=1, activation_fn=activation, scope='{}/decoder/conv_{}'.format(self.scoped_name, 3))
        resnet = tf.contrib.layers.conv2d(resnet, 128, 3, stride=1, activation_fn=None, scope='{}/decoder/conv_{}'.format(self.scoped_name, 4))
        inet = tf.add(inet, resnet, name='{}/decoder/sum_b{}'.format(self.scoped_name, 1))
        self.log('res block {}'.format(inet.shape))

        resnet = tf.contrib.layers.conv2d(inet, 128, 3, stride=1, activation_fn=activation, scope='{}/decoder/conv_{}'.format(self.scoped_name, 5))
        resnet = tf.contrib.layers.conv2d(resnet, 128, 3, stride=1, activation_fn=None, scope='{}/decoder/conv_{}'.format(self.scoped_name, 6))
        inet = tf.add(inet, resnet, name='{}/decoder/sum_c{}'.format(self.scoped_name, 2))
        self.log('res block {}'.format(inet.shape))

        inet = tf.contrib.layers.conv2d(inet, 256, 3, stride=1, activation_fn=activation, scope='{}/decoder/tconv_{}'.format(self.scoped_name, 7))
        self.log('conv:1 {} + {} activation'.format(inet.shape, self._h.activation))
        inet = tf.depth_to_space(inet, 2, name='{}/decoder/d2s_{}'.format(self.scoped_name, 7))
        self.log('dts {}'.format(inet.shape))

        inet = tf.contrib.layers.conv2d(inet, 12, 3, stride=1, activation_fn=None, scope='{}/decoder/tconv_{}'.format(self.scoped_name, 8))
        self.log('conv:1 {} + {} activation'.format(inet.shape, None))
        inet = tf.depth_to_space(inet, 2, name='{}/decoder/d2s_{}'.format(self.scoped_name, 8))
        self.log('dts {}'.format(inet.shape))

        with tf.name_scope('{}/decoder/denormalization'.format(self.scoped_name)):
            y = (inet + 1) / 2
            self.log('denorm: {}'.format(y.shape))

        y = tf.identity(y, name="y")

        self.y = y
        self.latent = latent

    @property
    def model_code(self):
        parameter_summary = []

        if hasattr(self, 'latent_shape'):
            parameter_summary.append('x'.join(str(x) for x in self.latent_shape[1:]))

        parameter_summary.append('r:{}'.format(self._h.rounding))
        parameter_summary.append(
            'Q+{}bpf'.format(self.latent_bpf) if self.train_codebook else 'Q-{}bpf'.format(self.latent_bpf))
        parameter_summary.append('S+' if self.scale_latent else 'S-')
        if self.entropy_weight is not None:
            parameter_summary.append('H+{:.2f}'.format(self.entropy_weight))

        return '{}/{}'.format(super().model_code, '-'.join(parameter_summary))

    def get_parameters(self):
        params = super().get_parameters()
        params.update(self._h.to_json())
        return params
