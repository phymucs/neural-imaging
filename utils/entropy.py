#%%
import sys
sys.path.append('..')

import numpy as np
import matplotlib.pyplot as plt

#%%

def quantize(x, codebook, v=100, sigma=5, dtype=np.float64):
    eps = 1e-72
    
    codebook = codebook.reshape((1, -1)).astype(dtype)
    values = x.reshape((-1, 1)).astype(dtype)

    if v <= 0:
        # Gaussian soft quantization
        weights = np.exp(-sigma * np.power(values - codebook, 2))
    else:
        # t-Student soft quantization
        dff = sigma * (values - codebook)
        weights = np.power((1 + np.power(dff, 2)/v), -(v+1)/2)
    
    weights = (weights + eps) / (eps + np.sum(weights, axis=1, keepdims=True))
    
    assert(weights.shape[1] == np.prod(codebook.shape))

    soft = np.matmul(weights, codebook.T)
    soft = soft.reshape(x.shape)            

    hard = codebook.reshape((-1,))[np.argmax(weights, axis=1)]
    hard = hard.reshape(x.shape)     
    
    histogram = np.mean(weights, axis=0)
    histogram = np.clip(histogram, 1e-9, np.finfo(np.float64).max)
    histogram = histogram / np.sum(histogram)
    entropy = - np.sum(histogram * np.log2(histogram))

    return soft, hard, histogram, entropy, weights

#%% Single example

c_max = 5

# Generate random data
# X = 3 * np.random.normal(size=(10000,1))
X = np.random.laplace(size=(2000,1), scale=2)

codebook = np.arange(-c_max, c_max+1, 1)

# Standard rounding / histogram / entropy
X_rnd = np.round(X).clip(-c_max, c_max)
hist = np.zeros_like(codebook)
unique, counts = np.unique(X_rnd.astype(np.int), return_counts=True)
indices = np.where(np.abs(codebook.reshape((-1,1)) - unique.reshape((1,-1))) == 0)[0]
hist[indices] = counts
hist = hist.clip(1)
hist = hist / hist.sum()
entropy_real = - np.sum(hist * np.log2(hist))

# Soft approximations
X_soft, X_hard, histogram, entropy, weights = quantize(X, codebook, v=0, sigma=5)

fig, axes = plt.subplots(2, 3, squeeze=False, figsize=(20,9))
axes[0, 0].plot(X_rnd, X_hard, '.')
axes[0, 0].plot([-c_max, c_max], [-c_max, c_max], ':')
axes[0, 0].set_title('Standard vs estimated hard quantization')
axes[0, 0].set_xlabel('standard rounding')
axes[0, 0].set_ylabel('hard estimate')


axes[0, 1].plot(X, X_soft, '.')
axes[0, 1].plot(X, X_rnd, '.')
axes[0, 1].set_title('Soft quantization vs input')
axes[0, 1].legend(['soft estimate', 'real quantization'])

axes[1, 0].plot(codebook, histogram, '.-')
axes[1, 0].plot(codebook, hist, '.-')
axes[1, 0].set_title('Histograms: real vs estimated')
axes[1, 0].legend(['soft estimate', 'real histogram'])

axes[1, 1].loglog(histogram, hist, '.')
axes[1, 1].loglog([0, 1], [0, 1], ':')
axes[1, 1].set_xlim([hist.min(), hist.max()])
axes[1, 1].set_ylim([hist.min(), hist.max()])
axes[1, 1].set_title('Histogram bins: real vs estimated')

axes[0, 2].imshow(weights[0:c_max*3])
axes[1, 2].remove()

quant_h_error = np.mean(np.abs(X_rnd - X_hard))
quant_s_error = np.mean(np.abs(X_rnd - X_soft))
hist_error = np.mean(np.abs(histogram - hist))
kld = - np.sum(hist * np.log2(histogram / hist))

print('Quantization error (hard) : {:.4f}'.format(quant_h_error))
print('Quantization error (soft) : {:.4f}'.format(quant_s_error))
print('Histogram bin error       : {:.4f}'.format(hist_error))
print('Entropy                   : {:.4f}'.format(entropy_real))
print('Entropy (soft)            : {:.4f}'.format(entropy))
print('Entropy error             : {:.2f}'.format(np.abs(entropy_real - entropy)))
print('Entropy error             : {:.3f}%'.format(100 * np.abs(entropy_real - entropy) / entropy_real))
print('Kullback-Leibler div.     : {:.4f}'.format(kld))


#%%

def estimate_errors(X, codebook, v=100, sigma=5):
    # Standard rounding / histogram / entropy
    x_rnd = np.round(X).clip(-5, 5)
    histogram = np.zeros_like(codebook)
    unique, counts = np.unique(x_rnd.astype(np.int), return_counts=True)
    indices = np.where(np.abs(codebook.reshape((-1,1)) - unique.reshape((1,-1))) == 0)[0]
    histogram[indices] = counts
    histogram = histogram.clip(1)
    histogram = histogram / histogram.sum()
    hard_entropy = - np.sum(histogram * np.log2(histogram))
    
    # Soft approximations
    _, _, _, soft_entropy, _ = quantize(X, codebook, v, sigma)

    entropy_error = np.abs(hard_entropy - soft_entropy)
    
    return hard_entropy, soft_entropy, entropy_error

#%% Large synthetic data experiment

v = 0
sigma = 5
n_scales = 500
n_samples = 1000
distribution = 'Laplace'
codebook = np.arange(-c_max, c_max+1, 1)

data = np.zeros((5, n_scales))
data[0] = np.linspace(0.01, 10, n_scales)

for i, scale in enumerate(data[0]):
    
    if distribution == 'Laplace':
        X = np.random.laplace(size=(n_samples, 1), scale=scale)
    elif distribution == 'Gaussian':
        X = scale * np.random.normal(size=(n_samples, 1))
        
    data[1:-1, i] = estimate_errors(X, codebook, v, sigma)

data[-1] = 100 * data[3] / data[1]

fig, axes = plt.subplots(1, 3, squeeze=False, figsize=(15,4))
axes[0, 0].plot(data[0], data[3], '.', alpha=0.5, markersize=3)
axes[0, 0].set_xlabel('{} distribution scale'.format(distribution))
axes[0, 0].set_ylabel('Absolute error')

axes[0, 1].plot(data[0], data[4], '.', alpha=0.5, markersize=3)
axes[0, 1].set_xlabel('{} distribution scale'.format(distribution))
axes[0, 1].set_ylabel('Relative error [%]')

axes[0, 2].plot(data[1], data[2], '.', alpha=0.5, markersize=5)
axes[0, 2].plot([0, 5], [0, 5], ':')
axes[0, 2].set_xlim([-0.05, 1.05*max(data[1])])
axes[0, 2].set_ylim([-0.05, 1.05*max(data[1])])
axes[0, 2].set_xlabel('Real entropy')
axes[0, 2].set_ylabel('Soft estimate')

fig.suptitle('Kernel: {}, sigma={}'.format('gaussian' if v == 0 else 't-Student({})'.format(v), sigma))

# %% Hyper-parameter search

n_scales = 500
n_samples = 1000
distribution = 'Laplace'

vs = [0, 5, 10, 25, 50, 100]
sig = [5, 10, 25, 50]

fig, axes = plt.subplots(len(vs), len(sig), sharex=True, sharey=True, squeeze=False, figsize=(5 * len(sig), 3 * len(vs)))

for n, v in enumerate(vs):
    for m, s in enumerate(sig):

        data = np.zeros((5, n_scales))
        data[0] = np.linspace(0.01, 10, n_scales)
        
        for i, scale in enumerate(data[0]):
            
            if distribution == 'Laplace':
                X = np.random.laplace(size=(n_samples, 1), scale=scale)
            elif distribution == 'Gaussian':
                X = scale * np.random.normal(size=(n_samples, 1))

            data[1:-1, i] = estimate_errors(X, codebook, v, s)

        data[-1] = 100 * data[3] / data[1]

        axes[n, m].plot(data[0], data[4], '.', alpha=0.25, markersize=5)
        if v == vs[-1]:
            axes[n, m].set_xlabel('{} distribution scale'.format(distribution))
        if m == 0:
            axes[n, m].set_ylabel('Relative entropy error [%]')
        axes[n, m].set_title('Kernel: {}, sigma={} -> {:.2f}'.format('gaussian' if v == 0 else 't-Student({})'.format(v), s, np.mean(data[4])))

# %% Real compression model and real images

from compression import afi
from helpers import dataset, utils

dcn_presets = {
    '4k': '../data/raw/dcn/entropy/TwitterDCN-4096D/16x16x16-r:soft-codebook-Q-5.0bpf-S+-H+250.00',
    '8k': '../data/raw/dcn/entropy/TwitterDCN-8192D/16x16x32-r:soft-codebook-Q-5.0bpf-S+-H+250.00',
    '16k': '../data/raw/dcn/entropy/TwitterDCN-16384D/16x16x64-r:soft-codebook-Q-5.0bpf-S+-H+250.00'
}

# %%

data = dataset.IPDataset('../data/clic256', n_images=35, v_images=0, load='y')
dcn = afi.restore_model(dcn_presets['8k'])

# %%

n_epochs = data.count_training * 10

results = np.zeros((2, n_epochs))

for epoch in range(n_epochs):

    batch_x = data.next_training_batch(epoch % data.count_training, 1, 128)
    batch_z = dcn.compress(batch_x)
    results[0, epoch] = utils.entropy(batch_z, dcn.get_codebook())
    results[1, epoch] = dcn.sess.run(dcn.entropy, feed_dict={dcn.x: batch_x})

# %%

plt.plot(results[0], results[1], '.', alpha=0.1, markersize=5)
plt.plot([0, 5], [0, 5], ':')
plt.xlim([-0.05, 1.05*max(results[1])])
plt.ylim([-0.05, 1.05*max(results[1])])
plt.xlabel('Real entropy')
plt.ylabel('Soft estimate')
plt.title('Real images + {}'.format(dcn.model_code))