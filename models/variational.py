import tensorflow as tf
import tensorflow_probability as tfp
import numpy as np
import collections
import flows
from flows.glow import GlowFlow
from tqdm import tqdm
from tensorflow.keras import Model

class VariationalModel(tf.Module):
    def __init__(self,
                 estimator: tf.Module,
                 parameterizer,
                 transform: flows.Transform=flows.Identity(),
                 num_bins=None,
                 optimizer=tf.keras.optimizers.Adamax(lr=1.0E-3),
                 clip_grads=True):
        """
        Creates a generalized, trainable model for variational inference.
        
        estimator     : a callable tf.Module (or Keras Model) which represents an inference function
                        f: (X -> Theta) such that X is the model inputs and Theta is a tensor of
                        distribution parameters.
        parameterizer : a function f: (Theta -> tfp.distributions.Distribution) which parameterizes a
                        variational distribution from the parameter values produced by 'estimator'
        transform     : a bijective transform to be applied to the initial variational density
        num_bins      : for discrete input spaces: number of discretized bins; i.e. num_bins = 2^(num_bits)
        optimizer     : optimizer to use during training
        clip_grads    : True if gradients should be clipped to the global norm, False otherwise
        """
        self.estimator = estimator
        self.parameterizer = parameterizer
        self.transform = transform
        self.num_bins = num_bins
        self.optimizer = optimizer
        self.clip_grads = clip_grads
        self.scale_factor = np.log2(num_bins) if num_bins is not None else 1.0
        self.input_shape = None
        
    def _parameterize(self, x):
        params = self.estimator(x)
        variational_dist = self.parameterizer(params)
        self.input_shape = variational_dist.batch_shape + variational_dist.event_shape
        if self.transform is not None and not self.transform.is_initialized():
            # initialize transform/flow if not already initialized
            with tf.init_scope():
                self.transform.initialize(self.input_shape)
        return variational_dist
    
    def _preprocess(self, x):
        if self.num_bins is not None:
            x += tf.random.uniform(x.shape, 0, 1./self.num_bins)
        return x
    
    def initialize(self, input_shape):
        self.input_shape = input_shape
        self.transform.initialize(input_shape)
        
    def eval_batch(self, x, y):
        num_elements = tf.cast(y.shape[1]*y.shape[2]*y.shape[3], tf.float32)
        prior = self._parameterize(x)
        z, ldj = self.transform.inverse(y)
        prior_log_probs = tf.math.reduce_sum(prior.log_prob(z), axis=[1,2,3])
        log_probs = prior_log_probs + ldj
        nll_loss = -(log_probs - self.scale_factor*num_elements) / num_elements
        return nll_loss, log_probs, prior_log_probs, ldj
        
    def train_batch(self, x, y):
        """
        Performs a single iteration of mini-batch SGD on input x.
        Returns loss, nll, prior, ildj[, grad_norm]
                where loss is the total optimized loss (including regularization),
                nll is the averaged negative log likelihood component,
                prior is the averaged prior negative log likelihodd,
                ildj is the inverse log det jacobian,
                and, if clip_grads is True, grad_norm is the global max gradient norm
        """
        with tf.GradientTape() as tape:
            nll_loss, log_probs, prior_log_probs, ldj = self.eval_batch(x, y)
            nll_loss = tf.math.reduce_mean(nll_loss)
            reg_losses = self.estimator.get_losses_for(None) if isinstance(self.estimator, Model) else []
            reg_losses += [self.transform._regularization_loss()]
            objective = nll_loss + tf.math.add_n(reg_losses)
            gradients = tape.gradient(objective, self.trainable_variables)
            if self.clip_grads:
                gradients, grad_norm = tf.clip_by_global_norm(gradients, 1.0)
            self.optimizer.apply_gradients(zip(gradients, self.trainable_variables))
        num_elements = tf.cast(y.shape[1]*y.shape[2]*y.shape[3], tf.float32)
        prior_log_probs = -tf.math.reduce_mean(prior_log_probs / num_elements)
        ldj = tf.math.reduce_mean(ldj) / num_elements
        return objective, nll_loss, prior_log_probs, ldj
            
    def train(self, train_data: tf.data.Dataset, steps_per_epoch, num_epochs=1, supervised=False,
              validation_data: tf.data.Dataset=None, validation_steps=1):
        train_data = train_data.take(steps_per_epoch).repeat(num_epochs)
        if validation_data is not None:
            validation_data = validation_data.repeat(num_epochs)
        with tqdm(total=steps_per_epoch*num_epochs) as prog:
            hist = collections.deque(maxlen=steps_per_epoch)
            validation_hist = collections.deque(maxlen=steps_per_epoch)
            for epoch in range(num_epochs):
                for batch in train_data.take(steps_per_epoch):
                    params = batch if supervised else [tf.zeros(batch.shape), batch]
                    loss, nll, prior, _ = self.train_batch(*params)
                    hist.append((loss, nll, prior))
                    prog.update(1)
                    prog.set_postfix({'epoch': epoch,
                                      'loss': np.mean([record[0] for record in hist]),
                                      'nll': np.mean([record[1] for record in hist]),
                                      'prior': np.mean([record[2] for record in hist]),
                                      'test_nll': np.mean([record[0] for record in validation_hist]) \
                                                  if len(validation_hist) > 0 else '-',
                                      'test_prior': np.mean([record[1] for record in validation_hist]) \
                                                  if len(validation_hist) > 0 else '-'})
                if validation_data is None:
                    continue
                validation_hist.clear()
                for batch in validation_data.take(validation_steps):
                    params = batch if has_y else [batch]
                    nll, _, prior, _ = self.eval_batch(*params)
                    validation_hist.append((nll, prior))
                
    def predict_mean(self, x=None):
        x = tf.zeros(self.input_shape) if x is None else x
        params = self.estimator(x)
        prior = self.parameterizer(params)
        z = prior.mean()
        x, _ = self.transform.forward(z)
        return x
                
    def sample(self, x=None):
        x = tf.zeros(self.input_shape) if x is None else x
        params = self.estimator(x)
        prior = self.parameterizer(params)
        z = prior.sample()
        x, _ = self.transform.forward(z)
        return x
    
    def distribution(self, x=None, invert=False):
        x = tf.zeros(self.input_shape) if x is None else x
        transform = flows.TransformBijector(self.transform)
        params = self.estimator(x)
        prior = self.parameterizer(params)
        return transform(prior)
        
### Miscellaneous utils for standalone amortized VI ###

def nll_loss(distribution_fn):
    def nll(y_true, y_pred):
        def log_prob(dist: tfp.distributions.Distribution):
            return dist.log_prob(y_true)
        dist = tfp.layers.DistributionLambda(distribution_fn, log_prob)
        nll = -dist(y_pred)
        return tf.reduce_mean(nll, axis=-1)
    return nll

def parameterize(model: Model, distribution_fn):
    model.predict_mean = lambda x: distribution_fn(model.predict(x)).mean()
    model.predict_q = lambda x, q: distribution_fn(model.predict(x)).quantile(q)
    model.sample = lambda x: distribution_fn(model.predict(x)).sample()
    return model