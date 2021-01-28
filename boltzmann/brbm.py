"""Bernoulli restricted Boltzmann machine."""

import numpy as np
import tensorflow as tf
from collections import defaultdict
from typing import List


def outer(x: tf.Tensor, y: tf.Tensor):
  return tf.expand_dims(x, axis=-1) * tf.expand_dims(y, axis=-2)


def random(shape: List[int]):
  return tf.random.uniform(shape=shape, minval=0., maxval=1.)


def expect(x: tf.Tensor):
  return tf.reduce_mean(x, axis=0)


class Initializer:

  @property
  def kernel(self):
    return NotImplemented
  
  @property
  def ambient_bias(self):
    return NotImplemented

  @property
  def latent_bias(self):
    return NotImplemented


class GlorotInitializer(Initializer):

  def __init__(self, samples: tf.Tensor, eps: float = 1e-8):
    self.samples = samples
    self.eps = eps

  @property
  def kernel(self):
    return tf.initializers.glorot_normal()
  
  @property
  def ambient_bias(self):

    def initializer(_, dtype):
      b = 1 / (expect(self.samples) + self.eps)
      return tf.cast(b, dtype)

    return initializer

  @property
  def latent_bias(self):
    return tf.initializers.zeros()


class HintonInitializer(Initializer):

  def __init__(self, samples: tf.Tensor, eps: float = 1e-8):
    self.samples = samples
    self.eps = eps

  @property
  def kernel(self):
    return tf.random_normal_initializer(stddev=1e-2)
  
  @property
  def ambient_bias(self):
    p = expect(self.samples)

    def initializer(_, dtype):
      b = tf.math.log(p + self.eps) - tf.math.log(1 - p + self.eps)
      return tf.cast(b, dtype)

    return initializer

  @property
  def latent_bias(self):
    return tf.initializers.zeros()


def create_variable(name: str,
                    shape: List[int],
                    initializer: Initializer,
                    dtype: str = 'float32',
                    trainable: bool = True):
  init_value = initializer(shape, dtype)
  return tf.Variable(init_value, trainable=trainable, name=name)


class BernoulliRBM:

  def __init__(self,
               ambient_size: int,
               latent_size: int,
               initializer: Initializer):
    self.ambient_size = ambient_size
    self.latent_size = latent_size
    self.initializer = initializer

    self.kernel = create_variable(
        name='kernel',
        shape=[ambient_size, latent_size],
        initializer=self.initializer.kernel,
    )
    self.latent_bias = create_variable(
        name='latent_bias',
        shape=[latent_size],
        initializer=self.initializer.latent_bias,
    )
    self.ambient_bias = create_variable(
        name='ambient_bias',
        shape=[ambient_size],
        initializer=self.initializer.ambient_bias,
    )


def activate(prob: tf.Tensor, stochastic: bool):
  if not stochastic:
    y = tf.where(prob >= 0.5, 1, 0)
  else:
    y = tf.where(random(prob.shape) <= prob, 1, 0)
  return tf.cast(y, prob.dtype)


def prob_latent_given_ambient(rbm: BernoulliRBM, ambient: tf.Tensor):
  W, b, x = rbm.kernel, rbm.latent_bias, ambient
  a = x @ W + b
  return tf.sigmoid(a)


def prob_ambient_given_latent(rbm: BernoulliRBM, latent: tf.Tensor):
  W, v, h = rbm.kernel, rbm.ambient_bias, latent
  a = h @ tf.transpose(W) + v
  return tf.sigmoid(a)


def relax(rbm: BernoulliRBM, ambient: tf.Tensor, max_iter: int, tol: float):
  for step in tf.range(max_iter):
    latent_prob = prob_latent_given_ambient(rbm, ambient)
    latent = activate(latent_prob, False)
    new_ambient_prob = prob_ambient_given_latent(rbm, latent)
    new_ambient = activate(new_ambient_prob, False)
    if tf.reduce_max(tf.abs(new_ambient - ambient)) < tol:
      break
    ambient = new_ambient
  return ambient, step


def get_energy(rbm: BernoulliRBM, ambient: tf.Tensor, latent: tf.Tensor):
  x, h = ambient, latent
  W, b, v = rbm.kernel, rbm.latent_bias, rbm.ambient_bias
  energy = (
      - tf.reduce_sum(x @ W * h, axis=-1)
      - tf.reduce_mean(h * b, axis=-1)
      - tf.reduce_mean(x * v, axis=-1)
  )
  return energy


def init_fantasy_latent(rbm: BernoulliRBM, num_samples: int):
  p = random([num_samples, rbm.latent_size])
  return tf.where(p >= 0.5, 1., 0.)


def get_grads_and_vars(rbm: BernoulliRBM,
                       real_ambient: tf.Tensor,
                       fantasy_latent: tf.Tensor):
  real_latent_prob = prob_latent_given_ambient(rbm, real_ambient)
  real_latent = activate(real_latent_prob, True)
  fantasy_ambient_prob = prob_ambient_given_latent(rbm, fantasy_latent)
  fantasy_ambient = activate(fantasy_ambient_prob, True)

  grad_kernel = (
      expect(outer(fantasy_ambient_prob, fantasy_latent))
      - expect(outer(real_ambient, real_latent))
  )
  grad_latent_bias = expect(fantasy_latent) - expect(real_latent)
  grad_ambient_bias = expect(fantasy_ambient) - expect(real_ambient)

  return [
      (grad_kernel, rbm.kernel),
      (grad_latent_bias, rbm.latent_bias),
      (grad_ambient_bias, rbm.ambient_bias),
  ]


def contrastive_divergence(rbm: BernoulliRBM,
                           fantasy_latent: tf.Tensor,
                           mc_steps: int):
  for _ in tf.range(mc_steps):
    fantasy_ambient_prob = prob_ambient_given_latent(rbm, fantasy_latent)
    fantasy_ambient = activate(fantasy_ambient_prob, True)
    fantasy_latent_prob = prob_latent_given_ambient(rbm, fantasy_ambient)
    fantasy_latent = activate(fantasy_latent_prob, True)
  return fantasy_latent


class History:

  def __init__(self):
    self.logs = defaultdict(dict)
  
  def log(self, step: int, key: str, value: object):
    try:  # maybe a tf.Tensor
      value = value.numpy()
    except AttributeError:
      pass
    self.logs[step][key] = value
  
  def show(self, step: int, keys: List[str] = None):
    if keys is None:
      keys = list(self.logs[step])
    
    aspects = []
    for k in keys:
      v = self.logs[step].get(k, None)
      if isinstance(v, (float, np.floating)):
        v = f'{v:.5f}'
      elif isinstance(v, str):
        pass
      else:
        raise ValueError(f'Type {type(v)} is temporally not supported.')
      aspects.append(f'{k}: {v}')

    show_str = ' - '.join([f'step: {step}'] + aspects)
    return show_str


def train(rbm: BernoulliRBM,
          optimizer: tf.optimizers.Optimizer,
          dataset: tf.data.Dataset,
          fantasy_latent: tf.Tensor,
          mc_steps: int = 1,
          history: History = None):
  for step, real_ambient in enumerate(dataset):
    grads_and_vars = get_grads_and_vars(rbm, real_ambient, fantasy_latent)
    optimizer.apply_gradients(grads_and_vars)
    fantasy_latent = contrastive_divergence(rbm, fantasy_latent, mc_steps)
  
    if history is not None and step % 10 == 0:
      log_and_show_internal_information(
          history, rbm, step, real_ambient, fantasy_latent)

  return fantasy_latent


def log_and_show_internal_information(
      history, rbm, step, real_ambient, fantasy_latent):
  real_latent = activate(prob_latent_given_ambient(rbm, real_ambient), False)
  recon_ambient = activate(prob_ambient_given_latent(rbm, real_latent), False)

  mean_energy = tf.reduce_mean(get_energy(rbm, real_ambient, real_latent))
  recon_error = tf.reduce_mean(
      tf.cast(recon_ambient == real_ambient, 'float32'))
  latent_on_ratio = tf.reduce_mean(real_latent)
  
  def stats(x, name):
    mean, var = tf.nn.moments(x, axes=range(len(x.shape)))
    std = tf.sqrt(var)
    history.log(step, f'{name}', f'{mean:.5f} ({std:.5f})')
  
  history.log(step, 'mean energy', mean_energy)
  history.log(step, 'recon error', recon_error)
  history.log(step, 'latent-on ratio', latent_on_ratio)

  stats(rbm.kernel, 'kernel')
  stats(rbm.ambient_bias, 'ambientbias')
  stats(rbm.latent_bias, 'latent bias')

  print(history.show(step), end='\r', flush=True)


if __name__ == '__main__':

  from mnist import load_mnist

  image_size = (16, 16)
  (X, _), _ = load_mnist(image_size=image_size, binarize=True, minval=0, maxval=1)

  ambient_size = image_size[0] * image_size[1]
  latent_size = 64
  batch_size = 128
  dataset = tf.data.Dataset.from_tensor_slices(X)
  dataset = dataset.shuffle(10000).repeat(10).batch(batch_size)
  rbm = BernoulliRBM(ambient_size, latent_size, HintonInitializer(X))
  fantasy_latent = init_fantasy_latent(rbm, batch_size)
  optimizer = tf.optimizers.Adam()
  history = History()
  fantasy_latent = train(rbm, optimizer, dataset, fantasy_latent,
                         history=history)