# Lint as: python2, python3
# Copyright 2020 The TensorFlow Probability Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Logistic regression models."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools

from tensorflow_probability.python.internal.backend.jax.compat import v2 as tf
from tensorflow_probability.python.bijectors import _jax as tfb
from tensorflow_probability.python.distributions import _jax as tfd

from tensorflow_probability.python.experimental.inference_gym.internal._jax import data
from tensorflow_probability.python.experimental.inference_gym.targets._jax import bayesian_model
from tensorflow_probability.python.experimental.inference_gym.targets._jax import model

__all__ = [
    'GermanCreditNumericLogisticRegression',
    'LogisticRegression',
]


def _add_bias(features):
  return tf.concat([features, tf.ones([tf.shape(features)[0], 1])], axis=-1)


class LogisticRegression(bayesian_model.BayesianModel):
  """Bayesian logistic regression with a Gaussian prior."""

  def __init__(
      self,
      train_features,
      train_labels,
      test_features=None,
      test_labels=None,
      name='logistic_regression',
      pretty_name='Logistic Regression',
  ):
    """Construct the logistic regression model.

    Args:
      train_features: Floating-point `Tensor` with shape `[num_train_points,
        num_features]`. Training features.
      train_labels: Integer `Tensor` with shape `[num_train_points]`. Training
        labels.
      test_features: Floating-point `Tensor` with shape `[num_test_points,
        num_features]`. Testing features. Can be `None`, in which case
        test-related sample transformations are not computed.
      test_labels: Integer `Tensor` with shape `[num_test_points]`. Testing
        labels. Can be `None`, in which case test-related sample transformations
        are not computed.
      name: Python `str` name prefixed to Ops created by this class.
      pretty_name: A Python `str`. The pretty name of this model.

    Raises:
      ValueError: If `test_features` and `test_labels` are either not both
        `None` or not both specified.
    """
    with tf.name_scope(name):
      train_features = tf.convert_to_tensor(train_features, tf.float32)
      train_features = _add_bias(train_features)
      train_labels = tf.convert_to_tensor(train_labels)
      num_features = int(train_features.shape[1])

      self._prior_dist = tfd.Sample(tfd.Normal(0., 1.), num_features)

      def log_likelihood_fn(weights, features, labels, reduce_sum=True):
        """The log_likelihood function."""
        logits = tf.einsum('nd,...d->...n', features, weights)
        log_likelihood = tfd.Bernoulli(logits=logits).log_prob(labels)
        if reduce_sum:
          return tf.reduce_sum(log_likelihood, [-1])
        else:
          return log_likelihood

      self._train_log_likelihood_fn = functools.partial(
          log_likelihood_fn, features=train_features, labels=train_labels)

      sample_transformations = {
          'identity':
              model.Model.SampleTransformation(
                  fn=lambda params: params,
                  pretty_name='Identity',
              )
      }
      if (test_features is not None) != (test_labels is not None):
        raise ValueError('`test_features` and `test_labels` must either both '
                         'be `None` or both specified. Got: test_features={}, '
                         'test_labels={}'.format(test_features, test_labels))

      if test_features is not None and test_labels is not None:
        test_features = tf.convert_to_tensor(test_features, tf.float32)
        test_features = _add_bias(test_features)
        test_labels = tf.convert_to_tensor(test_labels)
        test_log_likelihood_fn = functools.partial(
            log_likelihood_fn, features=test_features, labels=test_labels)

        sample_transformations['test_nll'] = (
            model.Model.SampleTransformation(
                fn=test_log_likelihood_fn,
                pretty_name='Test NLL',
            ))
        sample_transformations['per_example_test_nll'] = (
            model.Model.SampleTransformation(
                fn=functools.partial(test_log_likelihood_fn, reduce_sum=False),
                pretty_name='Per-example Test NLL',
            ))

    super(LogisticRegression, self).__init__(
        default_event_space_bijector=tfb.Identity(),
        event_shape=self._prior_dist.event_shape,
        dtype=self._prior_dist.dtype,
        name=name,
        pretty_name=pretty_name,
        sample_transformations=sample_transformations,
    )

  def _prior_distribution(self):
    return self._prior_dist

  def log_likelihood(self, value):
    return self._train_log_likelihood_fn(value)


class GermanCreditNumericLogisticRegression(LogisticRegression):
  """Bayesian logistic regression with a Gaussian prior.

  This model uses the German Credit (numeric) data set [1].

  #### References

  1. https://archive.ics.uci.edu/ml/datasets/statlog+(german+credit+data)
  """

  def __init__(self):
    dataset = data.german_credit_numeric()
    del dataset['test_features']
    del dataset['test_labels']
    super(GermanCreditNumericLogisticRegression, self).__init__(
        name='german_credit_numeric_logistic_regression',
        pretty_name='German Credit Numeric Logistic Regression',
        **dataset)

