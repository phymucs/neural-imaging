# Neural Imaging Toolbox 

Authors: [Paweł Korus](http://kt.agh.edu.pl/~korus/) and [Nasir Memon](http://isis.poly.edu/memon/), New York University

A Python toolbox for training neural imaging pipelines (NIPs) with support for explicit optimization of content authentication capabilities at the end of content distribution channels. The general principle is illustrated below for a binary manipulation detection problem:

![schematic overview](./docs/schematic_overview.png)

The whole imaging and distribution channel is modeled as a fully differentiable [Tensorflow](https://www.tensorflow.org/) model. The camera is replaced with a convolutional neural network and optimized for:

- faithful development of color RGB images from RAW sensor measurements,
- reliable manipulation detection at the end of the distribution channel.

The NIP is used to replace most of the steps of the standard pipeline:     

![neural imaging pipeline](./docs/nip_architectures_pipeline.png)

More information can be found in papers listed below.

**References:**

1. P. Korus, N. Memon, *Content Authentication for Neural Imaging Pipelines: End-to-end Optimization of Photo Provenance in Complex Distribution Channels*, CVPR'19, [arxiv:1812.01516](https://arxiv.org/abs/1812.01516) 
2. P. Korus, N. Memon, *Neural Imaging Pipelines - the Scourge or Hope of Forensics?*, 2019, [arXiv:1902.10707](https://arxiv.org/abs/1902.10707)

## Setup

The toolbox was written in Python 3. Follow the standard procedure to install dependencies.

```
> git clone https://github.com/pkorus/neural-imaging && cd neural-imaging.git
> pip3 install -r requirements.txt
> mkdir -p data/raw
```

#### Data Directory Structure

The `data/raw` directory is used for storing all data (input images, training pairs, trained model snapshots, etc.). The expected content looks as follow:

```
images/{Camera name}                            - RAW images (*.nef *.dng)
nip_training_data/{Camera name}                 - Bayer stacks (uint16 *.npy) and developed (*.png)
nip_model_snapshots/{Camera name}/{nip}         - NIP models (TF checkpoints)
nip_developed/{Camera name}/{pipeline or nip}   - NIP-developed images (*.png)
```

Training data can be prepared from input images with the `train_prepare_training_set.py` script (see details below).

## Getting Started

We followed a two-phase protocol to train our models:
- pre-training of the NIP models for faithful photo development,
- fine-tuning of the NIP models to optimize for reliable image manipulation detection.

Since the second phase uses both image fidelity and classification accuracy objectives, the enhanced NIP model can most likely be trained using the second phase only, but this has not been tested. 

### Phase I: Standard NIP Training

First, we need to extract NIP training data for a given camera. The training script looks for RAW images in `./data/raw/images/{camera}`. By default, 150 horizontal images will be taken. This step produces pairs of RGGB Bayer stacks (stored in `*.npy` files) and RGB optimization targets (`*.png`).

```
> python3 train_prepare_training_set.py --cam "Canon EOS 4D"
```

Then, we train selected NIP models (the `--nip` argument can be repeated). This step consumes (RGGB, RGB) training pairs and trains the NIP by optimizing the L2 loss on randomly sampled patches. By default, the 150 available images are split into 120/30 for training/validation.

```
> python3 train_nip.py --cam "Canon EOS 4D" --nip INet --nip UNet
```

If needed, additional parameters for the NIPs can be provided as a JSON string.

```
> python3 train_nip.py --cam "Nikon D7000" --nip INet --params '{"random_init": true}'
```

To validate the NIP models, you may wish to develop some images. The following command will develop all images in the data set. In this command, you can use all of the available imaging pipelines: `libRAW, Python, INet, DNet, UNet`.

```
> python3 develop_images.py {camera} {pipeline}
```

An example photograph developed with all of the available pipelines is shown below.  

![example NIP output](docs/nip_output_example.jpg)

To quickly test a selected NIP on a central image patch (128 x 128 px by default):

```
> python3 test_nip.py --cam "Canon EOS 5D" --nip INet
```

### Phase II: Training NIPs Optimized for Manipulation Detection

![training for optimized manipulation detection](docs/manipulation_detection_training_architecture.png)

The following command can be used for training NIPs optimized for image manipulation detection. In the current experiment, we follow a common evaluation protocol and train to distinguish between native camera output and 4 manipulation classes: sharpening, re-sampling, Gaussian filtering, and JPEG compression. (See figure above and our paper for details.) The basic usage is as follows:

```
> python3 train_manipulation.py --nip UNet --cam "Nikon D90" --cam "Nikon D7000"
```

The script relies on a single optimization objective with a regularized image fidelity term (see paper [2] for more details). It repeats the experiment 10 times for different values of the regularization strength. The optimization runs for a fixed number of epochs (1,000 by default) and saves results to the following directory:

```
data/raw/train_manipulation/{camera}/{nip}/lr-{NIP-regularization}/{run number}/
```

The script generates:
- `training.json` - JSON file with training progress and performance stats,
- `manip_validation_*.jpg` - visual presentation of training progress (change of loss, PSNR, etc. over time)
- `nip_validation_*.jpg` - current snapshot of patches developed by the NIP
- `models/{fan,*net}` - current snapshot of the models (both the NIP and the FAN)

**Plotting Results**

The results can be quickly inspected with the `results.py` script. For example, the following command shows the scatter plot with the trade-off between classification accuracy and image fidelity for the `UNet` model trained on `Nikon D90` :

```
> python3 results.py --nip UNet --cam "Nikon D90" scatter-psnr
```

![training for optimized manipulation detection](docs/scatterplot-nikon-d90.png)

To visualize variations of classification accuracy and image quality as the training progresses:

```
> python3 results.py --nip UNet --cam "Nikon D90" progress
```
![training for optimized manipulation detection](docs/progress-nikon-d90.png)

To show confusion matrices for all regularization strengths:

```
> python3 results.py --nip UNet --cam "Nikon D90" confusion
```
![training for optimized manipulation detection](docs/confusion-nikon-d90.png)

**Show Differences in NIP models**

This command shows differences between a UNet model trained normally (A) and with manipulation detection objectives (B). 

```
> python3 test_nip_compare.py --nip UNet --cam "Nikon D90" --b ./data/raw/train_manipulation/Nikon\ D90/UNet/lr-0.1000/000/models/ --image 16
```

![Differences between NIP models](docs/nip_differences.jpg)

## Available Pipelines

The toolbox currently provides the following pipelines:

- `libRAW` - uses the libRAW library to develop RAW images
- `Python` - simple Python implementation of a standard pipeline
- `INet`   - simple NIP which mimics step-by-step processing of the standard pipeline
- `UNet`   - the well known UNet network 
- `DNet`   - a medium-sized model adapted from a recent architecture for joing demosaicing and denoising

The standard pipelines are available in the `raw_api` module. Neural pipelines are available in `models/pipelines`. The UNet model was adapted from [Learning to See in the Dark](https://github.com/cchen156/Learning-to-See-in-the-Dark).

## Implementing New Pipelines

The toolbox pools available neural pipelines from the `models/pipelines` module. Implementation of new pipelines involves sub-classing `NIPModel` and providing implementations for the `construct_model` method and the `parameters` property. 

Network models are expected to use the provided input placeholder (`self.x`) and add attributes for model output (`self.y` and optionally `self.yy`). The standard output (`self.y`) should be clipped to [0,1]. For better learning stability, a non-clipped output can be provided (`self.yy`) - it will be automatically used for gradient computation. The models should use an optional string prefix (`self.label`) in variable names or named scopes. This facilitates the use of multiple NIPs in a single TF graph. 

## JPEG Approximation

The repository contains a differentiable model of JPEG compression which can be useful in other research as well (see `models.jpeg.DJPG`). The model expresses successive steps of the codec as  matrix multiplications or convolution layers (see papers for details) and supports the following approximations of DCT coefficient quantization:

- `None` - uses standard rounding (backpropagation not supported)
- `sin` - sinusoidal approximation of the rounding operator (allows for back-propagation)
- `soft` - uses standard rounding in the forward pass and sinusoidal approximation in the backward pass
- `harmonic` - a differentiable approximation with Taylor expansion 

See the test script `test_jpg.py` for a standalone usage example. The following plot compares image quality and generated outputs for various approximation modes.

![Differences between NIP models](docs/dJPEG.png)

## Forensics Analysis Network

Our Forensic Analysis Network (FAN) follows the state-of-the-art design principles and uses a constrained convolutional layer proposed in:

- Bayar, Belhassen, and Matthew C. Stamm. [Constrained convolutional neural networks: A new approach towards general purpose image manipulation detection.](https://ieeexplore.ieee.org/document/8335799) IEEE Transactions on Information Forensics and Security, 2018

While the original model used only the green channel, our FAN uses full RGB information for forensic analysis. See the `models.forensics.FAN` class for our Tensorflow implementation.

## Other Useful Scripts

- `confusion.py` - extracts confusion matrices from the experiments (LaTeX output supported)
- `summarize_nip.py` - extracts and summarizes performance stats for standalone NIP models

## Data Sources

In our experiments we used RAW images from publicly available datasets: 

- MIT-5k - [https://data.csail.mit.edu/graphics/fivek/](https://data.csail.mit.edu/graphics/fivek/)
- RAISE - [http://loki.disi.unitn.it/RAISE/](http://loki.disi.unitn.it/RAISE/)

## Usage and Citations

This code is provided for educational purposes and aims to facilitate reproduction of our results, and further research in this direction. We have done our best to  document, refactor, and test the code before publication. However, the toolbox is provided "as-is", without warranties of any kind.   

If you find any bugs or would like to contribute new models, training protocols, etc, please let us know. 

If you find this code useful in your work, please cite our papers:

```

@inproceedings{korus2019content,
  title={Content Authentication for Neural Imaging Pipelines: End-to-end Optimization of Photo Provenance in Complex Distribution Channels},
  author={Korus, Pawel and Memon, Nasir},
  booktitle={CVPR},
  year={2019}
}
```

```
@article{korus2019neural,
  title={Neural Imaging Pipelines - the Scourge or Hope of Forensics?},
  author={Korus, Pawel and Memon, Nasir},
  journal={arXiv preprint arXiv:1902.10707},
  year={2019}
}
```

## Related Work

A shortlist of related papers with global end-to-end ISP optimization:

- Eli Schwartz, Raja Giryes, Alex M. Bronstein, [DeepISP: Towards Learning an End-to-End Image Processing Pipeline](https://arxiv.org/abs/1801.06724), 2019 - optimization for low-light performance
- Chen Chen, Qifeng Chen, Jia Xu, Vladlen Koltun, [Learning to See in the Dark](https://arxiv.org/abs/1805.01934), 2018 - optimization for low-light performance
- Marc Levoy, Yael Pritch [Night Sight: Seeing in the Dark on Pixel Phones](https://ai.googleblog.com/2018/11/night-sight-seeing-in-dark-on-pixel.html), 2018 - low-light optimization in Pixel 3 phones
- Steven Diamond, Vincent Sitzmann, Stephen Boyd, Gordon Wetzstein, Felix Heide, [Dirty Pixels: Optimizing Image Classification Architectures for Raw Sensor Data](https://arxiv.org/abs/1701.06487), 2017 - optimization for high-level vision
- Haomiao Jiang, Qiyuan Tian, Joyce Farrell, Brian Wandell, [Learning the Image Processing Pipeline](https://ieeexplore.ieee.org/document/7944641), 2017 - learning ISPs for non-standard CFA patterns
- Gabriel Eilertsen, Joel Kronander, Gyorgy Denes, Rafał K. Mantiuk, Jonas Unger, [HDR image reconstruction from a single exposure using deep CNNs](http://hdrv.org/hdrcnn/), 2017 - HDR simulation from a single exposure
- Felix Heide et al., [FlexISP: A Flexible Camera Image Processing Framework](http://www.cs.ubc.ca/labs/imager/tr/2014/FlexISP/), 2014 - general ISP optimization framework for various low-level vision problems
