# Copyright 2018 The TensorFlow Probability Authors.
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
# ============================================================================
"""The `JointDistribution` base class."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import abc
import collections
import numpy as np
import six

from tensorflow_probability.python.internal.backend.numpy.compat import v2 as tf

from tensorflow_probability.python.bijectors._numpy import bijector as bijector_lib
from tensorflow_probability.python.bijectors._numpy import identity as identity_bijector
from tensorflow_probability.python.distributions._numpy import distribution as distribution_lib
from tensorflow_probability.python.internal._numpy import assert_util
from tensorflow_probability.python.internal._numpy import distribution_util
from tensorflow_probability.python.internal import docstring_util
from tensorflow_probability.python.internal._numpy import prefer_static


__all__ = [
    'JointDistribution',
]


CALLING_CONVENTION_DESCRIPTION = """
    The measure methods of `JointDistribution` (`log_prob`, `prob`, etc.)
    can be called either by passing a single structure of tensors or by using
    named args for each part of the joint distribution state. For example,

    ```python
    jd = tfd.JointDistributionSequential([
        tfd.Normal(0., 1.),
        lambda z: tfd.Normal(z, 1.)
    ], validate_args=True)
    jd.dtype
    # ==> [tf.float32, tf.float32]
    z, x = sample = jd.sample()
    # The following calling styles are all permissable and produce the exactly
    # the same output.
    assert (jd.{method}(sample) ==
            jd.{method}(value=sample) ==
            jd.{method}(z, x) ==
            jd.{method}(z=z, x=x) ==
            jd.{method}(z, x=x))

    # These calling possibilities also imply that one can also use `*`
    # expansion, if `sample` is a sequence:
    jd.{method}(*sample)
    # and similarly, if `sample` is a map, one can use `**` expansion:
    jd.{method}(**sample)
    ```

    `JointDistribution` component distributions names are resolved via
    `jd._flat_resolve_names()`, which is implemented by each `JointDistribution`
    subclass (see subclass documentation for details). Generally, for components
    where a name was provided---
    either explicitly as the `name` argument to a distribution or as a key in a
    dict-valued JointDistribution, or implicitly, e.g., by the argument name of
    a `JointDistributionSequential` distribution-making function---the provided
    name will be used. Otherwise the component will receive a dummy name; these
    may change without warning and should not be relied upon.

    Note: not all `JointDistribution` subclasses support all calling styles;
    for example, `JointDistributionNamed` does not support positional arguments
    (aka "unnamed arguments") unless the provided model specifies an ordering of
    variables (i.e., is an `collections.OrderedDict` or `collections.namedtuple`
    rather than a plain `dict`).

    Note: care is taken to resolve any potential ambiguity---this is generally
    possible by inspecting the structure of the provided argument and "aligning"
    it to the joint distribution output structure (defined by `jd.dtype`). For
    example,

    ```python
    trivial_jd = tfd.JointDistributionSequential([tfd.Exponential(1.)])
    trivial_jd.dtype  # => [tf.float32]
    trivial_jd.{method}([4.])
    # ==> Tensor with shape `[]`.
    {method_abbr} = trivial_jd.{method}(4.)
    # ==> Tensor with shape `[]`.
    ```

    Notice that in the first call, `[4.]` is interpreted as a list of one
    scalar while in the second call the input is a scalar. Hence both inputs
    result in identical scalar outputs. If we wanted to pass an explicit
    vector to the `Exponential` component---creating a vector-shaped batch
    of `{method}`s---we could instead write
    `trivial_jd.{method}(np.array([4]))`.

    Args:
      *args: Positional arguments: a `value` structure or component values
        (see above).
      **kwargs: Keyword arguments: a `value` structure or component values
        (see above). May also include `name`, specifying a Python string name
        for ops generated by this method.
  """


# Avoids name collision with measure function (`log_prob`, `prob`, etc.) args.
FORBIDDEN_COMPONENT_NAMES = ('value', 'name')


@six.add_metaclass(abc.ABCMeta)
class JointDistribution(distribution_lib.Distribution):
  """Joint distribution over one or more component distributions.

  This distribution enables both sampling and joint probability computation from
  a single model specification.

  A joint distribution is a collection of possibly interdependent distributions.

  **Note**: unlike other non-`JointDistribution` distributions in
  `tfp.distributions`, `JointDistribution.sample` (and subclasses) return a
  structure of  `Tensor`s rather than a `Tensor`.  A structure can be a `list`,
  `tuple`, `dict`, `collections.namedtuple`, etc. Accordingly
  `joint.batch_shape` returns a structure of `TensorShape`s for each of the
  distributions' batch shapes and `joint.batch_shape_tensor()` returns a
  structure of `Tensor`s for each of the distributions' event shapes. (Same with
  `event_shape` analogues.)

  #### Subclass Requirements

  Subclasses implement:

  - `_flat_sample_distributions`: returns two `list`-likes: the first being a
    sequence of `Distribution`-like instances the second being a sequence of
    `Tensor` samples, each one drawn from its corresponding `Distribution`-like
    instance. The optional `value` argument is either `None` or a `list`-like
    with the same `len` as either of the results.

  - `_model_flatten`: takes a structured input and returns a sequence.

  - `_model_unflatten`: takes a sequence and returns a structure matching the
    semantics of the `JointDistribution` subclass.

  Subclasses initialize:

  - `_single_sample_distributions`: an empty dictionary, which will hold a
      mapping from graph id to a prototypical list of component distributions
      sampled from the model.
  """

  def _get_single_sample_distributions(self, candidate_dists=None):
    """Returns a list of dists from a single sample of the model."""

    # If we have cached distributions with Eager tensors, return those.
    ds = self._single_sample_distributions.get(-1, None)
    if ds is not None and all([d is not None for d in ds]):
      return ds

    # Otherwise, retrieve or build distributions for the current graph.
    graph_id = -1 if tf.executing_eagerly() else id(tf.constant(True).graph)
    ds = self._single_sample_distributions.get(graph_id, None)
    if ds is None or any([d is None for d in ds]):
      ds = (candidate_dists if candidate_dists is not None else
            self._flat_sample_distributions(seed=42)[0])  # Seed for CSE.
      self._single_sample_distributions[graph_id] = ds
    return ds

  # Override `tf.Module`'s `_flatten` method to ensure that distributions are
  # instantiated, so that accessing `.variables` or `.trainable_variables` gives
  # consistent results.
  def _flatten(self, *args, **kwargs):
    self._get_single_sample_distributions()
    return super(JointDistribution, self)._flatten(*args, **kwargs)

  @abc.abstractmethod
  def _flat_sample_distributions(self, sample_shape=(), seed=None, value=None):
    raise NotImplementedError()

  @abc.abstractmethod
  def _model_unflatten(self, xs):
    raise NotImplementedError()

  @abc.abstractmethod
  def _model_flatten(self, xs):
    raise NotImplementedError()

  @property
  def dtype(self):
    """The `DType` of `Tensor`s handled by this `Distribution`."""
    return self._model_unflatten([
        d.dtype for d in self._get_single_sample_distributions()])

  @property
  def reparameterization_type(self):
    """Describes how samples from the distribution are reparameterized.

    Currently this is one of the static instances
    `tfd.FULLY_REPARAMETERIZED` or `tfd.NOT_REPARAMETERIZED`.

    Returns:
      reparameterization_type: `ReparameterizationType` of each distribution in
        `model`.
    """
    return self._model_unflatten([
        d.reparameterization_type
        for d in self._get_single_sample_distributions()])

  @property
  def batch_shape(self):
    """Shape of a single sample from a single event index as a `TensorShape`.

    May be partially defined or unknown.

    The batch dimensions are indexes into independent, non-identical
    parameterizations of this distribution.

    Returns:
      batch_shape: `tuple` of `TensorShape`s representing the `batch_shape` for
        each distribution in `model`.
    """
    return self._model_unflatten([
        d.batch_shape for d in self._get_single_sample_distributions()])

  def batch_shape_tensor(self, sample_shape=(), name='batch_shape_tensor'):
    """Shape of a single sample from a single event index as a 1-D `Tensor`.

    The batch dimensions are indexes into independent, non-identical
    parameterizations of this distribution.

    Args:
      sample_shape: The sample shape under which to evaluate the joint
        distribution. Sample shape at root (toplevel) nodes may affect the batch
        or event shapes of child nodes.
      name: name to give to the op

    Returns:
      batch_shape: `Tensor` representing batch shape of each distribution in
        `model`.
    """
    with self._name_and_control_scope(name):
      return self._model_unflatten(
          self._map_attr_over_dists(
              'batch_shape_tensor',
              dists=(self.sample_distributions(sample_shape)
                     if sample_shape else None)))

  @property
  def event_shape(self):
    """Shape of a single sample from a single batch as a `TensorShape`.

    May be partially defined or unknown.

    Returns:
      event_shape: `tuple` of `TensorShape`s representing the `event_shape` for
        each distribution in `model`.
    """
    # Caching will not leak graph Tensors since this is a static attribute.
    if not hasattr(self, '_cached_event_shape'):
      self._cached_event_shape = [
          d.event_shape
          for d in self._get_single_sample_distributions()]
    # Unflattening *after* retrieving from cache prevents tf.Module from
    # wrapping the returned value.
    return self._model_unflatten(self._cached_event_shape)

  def event_shape_tensor(self, sample_shape=(), name='event_shape_tensor'):
    """Shape of a single sample from a single batch as a 1-D int32 `Tensor`.

    Args:
      sample_shape: The sample shape under which to evaluate the joint
        distribution. Sample shape at root (toplevel) nodes may affect the batch
        or event shapes of child nodes.
      name: name to give to the op
    Returns:
      event_shape: `tuple` of `Tensor`s representing the `event_shape` for each
        distribution in `model`.
    """
    with self._name_and_control_scope(name):
      return self._model_unflatten(
          self._map_attr_over_dists(
              'event_shape_tensor',
              dists=(self.sample_distributions(sample_shape)
                     if sample_shape else None)))

  def sample_distributions(self, sample_shape=(), seed=None, value=None,
                           name='sample_distributions', **kwargs):
    """Generate samples and the (random) distributions.

    Note that a call to `sample()` without arguments will generate a single
    sample.

    Args:
      sample_shape: 0D or 1D `int32` `Tensor`. Shape of the generated samples.
      seed: Python integer seed for generating random numbers.
      value: `list` of `Tensor`s in `distribution_fn` order to use to
        parameterize other ("downstream") distribution makers.
        Default value: `None` (i.e., draw a sample from each distribution).
      name: name prepended to ops created by this function.
        Default value: `"sample_distributions"`.
      **kwargs: This is an alternative to passing a `value`, and achieves the
        same effect. Named arguments will be used to parameterize other
        dependent ("downstream") distribution-making functions. If a `value`
        argument is also provided, raises a ValueError.

    Returns:
      distributions: a `tuple` of `Distribution` instances for each of
        `distribution_fn`.
      samples: a `tuple` of `Tensor`s with prepended dimensions `sample_shape`
        for each of `distribution_fn`.
    """
    with self._name_and_control_scope(name):
      ds, xs = self._call_flat_sample_distributions(sample_shape, seed, value,
                                                    **kwargs)
      return self._model_unflatten(ds), self._model_unflatten(xs)

  def log_prob_parts(self, value, name='log_prob_parts'):
    """Log probability density/mass function.

    Args:
      value: `list` of `Tensor`s in `distribution_fn` order for which we compute
        the `log_prob_parts` and to parameterize other ("downstream")
        distributions.
      name: name prepended to ops created by this function.
        Default value: `"log_prob_parts"`.

    Returns:
      log_prob_parts: a `tuple` of `Tensor`s representing the `log_prob` for
        each `distribution_fn` evaluated at each corresponding `value`.
    """
    with self._name_and_control_scope(name):
      xs = self._map_measure_over_dists('log_prob', value)
      return self._model_unflatten(
          maybe_check_wont_broadcast(xs, self.validate_args))

  def prob_parts(self, value, name='prob_parts'):
    """Log probability density/mass function.

    Args:
      value: `list` of `Tensor`s in `distribution_fn` order for which we compute
        the `prob_parts` and to parameterize other ("downstream") distributions.
      name: name prepended to ops created by this function.
        Default value: `"prob_parts"`.

    Returns:
      prob_parts: a `tuple` of `Tensor`s representing the `prob` for
        each `distribution_fn` evaluated at each corresponding `value`.
    """
    with self._name_and_control_scope(name):
      xs = self._map_measure_over_dists('prob', value)
      return self._model_unflatten(
          maybe_check_wont_broadcast(xs, self.validate_args))

  def is_scalar_event(self, name='is_scalar_event'):
    """Indicates that `event_shape == []`.

    Args:
      name: Python `str` prepended to names of ops created by this function.

    Returns:
      is_scalar_event: `bool` scalar `Tensor` for each distribution in `model`.
    """
    with self._name_and_control_scope(name):
      return self._model_unflatten(
          [self._is_scalar_helper(shape, shape_tensor)  # pylint: disable=g-complex-comprehension
           for (shape, shape_tensor) in zip(
               self._model_flatten(self.event_shape),
               self._model_flatten(self.event_shape_tensor()))])

  def is_scalar_batch(self, name='is_scalar_batch'):
    """Indicates that `batch_shape == []`.

    Args:
      name: Python `str` prepended to names of ops created by this function.

    Returns:
      is_scalar_batch: `bool` scalar `Tensor` for each distribution in `model`.
    """
    with self._name_and_control_scope(name):
      return self._model_unflatten(
          self._map_attr_over_dists('is_scalar_batch'))

  def _log_prob(self, value):
    xs = self._map_measure_over_dists('log_prob', value)
    return sum(maybe_check_wont_broadcast(xs, self.validate_args))

  @distribution_util.AppendDocstring(kwargs_dict={
      'value': ('`Tensor`s structured like `type(model)` used to parameterize '
                'other dependent ("downstream") distribution-making functions. '
                'Using `None` for any element will trigger a sample from the '
                'corresponding distribution. Default value: `None` '
                '(i.e., draw a sample from each distribution).'),
      '**kwargs:': ('This is an alternative to passing a `value`, and achieves '
                    'the same effect. Named arguments will be used to '
                    'parameterize other dependent ("downstream") '
                    'distribution-making functions. See `value` for more '
                    'details. If a `value` argument is also provided, raises '
                    'a `ValueError`.')})
  def _sample_n(self, sample_shape, seed, value=None, **kwargs):
    if value is not None and kwargs:
      keywords = ', '.join(map(str, kwargs))
      raise ValueError('Supplied both `value` and keyword arguments to '
                       'parameterize sampling. Supplied keywords were: '
                       '{}'.format(keywords))
    _, xs = self._call_flat_sample_distributions(sample_shape, seed, value,
                                                 **kwargs)
    return self._model_unflatten(xs)

  def _map_measure_over_dists(self, attr, value):
    if any(x is None for x in tf.nest.flatten(value)):
      raise ValueError('No `value` part can be `None`; saw: {}.'.format(value))
    ds, xs = self._call_flat_sample_distributions(value=value, seed=42)
    return (getattr(d, attr)(x) for d, x in zip(ds, xs))

  def _map_attr_over_dists(self, attr, dists=None):
    dists = (self._get_single_sample_distributions()
             if dists is None else dists)
    return (getattr(d, attr)() for d in dists)

  def _call_flat_sample_distributions(
      self, sample_shape=(), seed=None, value=None, **kwargs):
    if (value is None) and kwargs:
      names = self._flat_resolve_names()
      kwargs.update({k: kwargs.get(k) for k in names})  # In place update
      value, unmatched_kwargs = _resolve_value_from_args(
          [],
          kwargs,
          dtype=self.dtype,
          flat_names=names,
          model_flatten_fn=self._model_flatten,
          model_unflatten_fn=self._model_unflatten)
      if unmatched_kwargs:
        join = lambda args: ', '.join(str(j) for j in args)
        kwarg_names = join(k for k, v in kwargs.items() if v is not None)
        dist_name_str = join(names)
        unmatched_str = join(unmatched_kwargs)
        raise ValueError(
            'Found unexpected keyword arguments. Distribution names '
            'are\n{}\nbut received\n{}\nThese names were '
            'invalid:\n{}'.format(dist_name_str, kwarg_names, unmatched_str))
    if value is not None:
      value = self._model_flatten(value)
    ds, xs = self._flat_sample_distributions(sample_shape, seed, value)

    if not sample_shape and value is None:
      # Maybe cache these distributions.
      self._get_single_sample_distributions(candidate_dists=ds)

    return ds, xs

  # Override the base method to capture *args and **kwargs, so we can
  # implement more flexible custom calling semantics.
  @docstring_util.expand_docstring(
      calling_convention_description=CALLING_CONVENTION_DESCRIPTION.format(
          method='log_prob', method_abbr='lp'))
  def log_prob(self, *args, **kwargs):  # pylint: disable=g-doc-args
    """Log probability density/mass function.

    ${calling_convention_description}

    Returns:
      log_prob: a `Tensor` of shape `sample_shape(x) + self.batch_shape` with
        values of type `self.dtype`.
    """
    kwargs['name'] = kwargs.get('name', 'log_prob')
    value, unmatched_kwargs = _resolve_value_from_args(
        args,
        kwargs,
        dtype=self.dtype,
        flat_names=self._flat_resolve_names(),
        model_flatten_fn=self._model_flatten,
        model_unflatten_fn=self._model_unflatten)
    return self._call_log_prob(value, **unmatched_kwargs)

  # Override the base method to capture *args and **kwargs, so we can
  # implement more flexible custom calling semantics.
  @docstring_util.expand_docstring(
      calling_convention_description=CALLING_CONVENTION_DESCRIPTION.format(
          method='prob', method_abbr='prob'))
  def prob(self, *args, **kwargs):  # pylint: disable=g-doc-args
    """Probability density/mass function.

    ${calling_convention_description}

    Returns:
      prob: a `Tensor` of shape `sample_shape(x) + self.batch_shape` with
        values of type `self.dtype`.
    """
    kwargs['name'] = kwargs.get('name', 'prob')
    value, unmatched_kwargs = _resolve_value_from_args(
        args,
        kwargs,
        dtype=self.dtype,
        flat_names=self._flat_resolve_names(),
        model_flatten_fn=self._model_flatten,
        model_unflatten_fn=self._model_unflatten)

    return self._call_prob(value, **unmatched_kwargs)

  def _flat_resolve_names(self, dummy_name='var'):
    """Resolves a name for each random variable in the model."""
    names = []
    dummy_idx = 1
    for d in self._get_single_sample_distributions():
      name = get_explicit_name_for_component(d)
      if name is None:
        # Wrapping dummy names with <> prevents users from passing them as
        # kwargs.
        name = '<{}{}>'.format(dummy_name, dummy_idx)
        dummy_idx += 1
      names.append(name)
    return names

  # We need to bypass base Distribution reshaping logic, so we
  # tactically implement the `_call_sample_n` redirector.  We don't want to
  # override the public level because then tfp.layers can't take generic
  # `Distribution.sample` as argument for the `convert_to_tensor_fn` parameter.
  def _call_sample_n(self, sample_shape, seed, name, value=None, **kwargs):
    with self._name_and_control_scope(name):
      return self._sample_n(
          sample_shape,
          seed=seed() if callable(seed) else seed,
          value=value,
          **kwargs)

  def _default_event_space_bijector(self):
    return _DefaultJointBijector(self)


def get_explicit_name_for_component(d):
  """Returns the explicitly-passed `name` of a Distribution, or None."""
  name = d.parameters.get('name', None)
  if name and d.__class__.__name__ in name:
    name = None

  if name in FORBIDDEN_COMPONENT_NAMES:
    raise ValueError('Distribution name "{}" is not allowed as a '
                     'JointDistribution component; please choose a different '
                     'name.'.format(name))
  return name


def _resolve_value_from_args(args,
                             kwargs,
                             dtype,
                             flat_names,
                             model_flatten_fn,
                             model_unflatten_fn):
  """Resolves a `value` structure matching `dtype` from a function call.

  This offers semantics equivalent to a Python callable `f(x1, x2, ..., xN)`,
  where `'x1', 'x2', ..., 'xN' = self._flat_resolve_names()` are the names of
  the model's component distributions. Arguments may be passed by position
  (`f(1., 2., 3.)`), by name (`f(x1=1., x2=2., x3=3.)`), or by a combination
  of approaches (`f(1., 2., x3=3.)`).

  Passing a `value` structure directly (as in `jd.log_prob(jd.sample())`) is
  supported by an optional `value` kwarg (`f(value=[1., 2., 3.])`), or by
  simply passing the value as the sole positional argument
  (`f([1., 2., 3.])`). For models having only a single component, a single
  positional argument that matches the structural type (e.g., a single Tensor,
  or a nested list or dict of Tensors) of that component is interpreted as
  specifying it; otherwise a single positional argument is interpreted as
  the overall `value`.

  Args:
    args: Positional arguments passed to the function being called.
    kwargs: Keyword arguments passed to the function being called.
    dtype: Nested structure of `dtype`s of model components.
    flat_names: Iterable of Python `str` names of model components.
    model_flatten_fn: Python `callable` that takes a structure and returns a
      list representing the flattened structure.
    model_unflatten_fn: Python `callable` that takes an iterable and returns a
      structure.
  Returns:
    value: A structure in which the observed arguments are arranged to match
      `dtype`.
    unmatched_kwargs: Python `dict` containing any keyword arguments that don't
      correspond to model components.
  Raises:
    ValueError: if the number of args passed doesn't match the number of
      model components, or if positional arguments are passed to a dict-valued
      distribution.
  """

  value = kwargs.pop('value', None)
  if value is not None:  # Respect 'value' as an explicit kwarg.
    return value, kwargs

  matched_kwargs = {k for k in flat_names if k in kwargs}
  unmatched_kwargs = {k: v for (k, v) in kwargs.items()
                      if k not in matched_kwargs}

  # If we have only a single positional arg, we need to disambiguate it by
  # examining the model structure.
  if len(args) == 1 and not matched_kwargs:
    if len(dtype) > 1:  # Model has multiple variables; arg must be a structure.
      return args[0], unmatched_kwargs
    # Otherwise the model has one variable. If its structure matches the arg,
    # interpret the arg as its value.
    first_component_dtype = model_flatten_fn(dtype)[0]
    try:
      # TODO(davmre): this assertion will falsely trigger if args[0] contains
      # nested lists that the user intends to be converted to Tensor. We should
      # try to relax it slightly (without creating false negatives).
      tf.nest.assert_same_structure(
          first_component_dtype, args[0], check_types=False)
      return model_unflatten_fn(args), unmatched_kwargs
    except (ValueError, TypeError):     # If RV doesn't match the arg, interpret
      return args[0], unmatched_kwargs  # the arg as a 'value' structure.

  num_components_specified = len(args) + len(kwargs) - len(unmatched_kwargs)
  if num_components_specified != len(flat_names):
    raise ValueError('Joint distribution expected values for {} components {}; '
                     'saw {} (from args {} and kwargs {}).'.format(
                         len(flat_names),
                         flat_names,
                         num_components_specified,
                         args,
                         kwargs))

  if args and (isinstance(dtype, dict) and not
               isinstance(dtype, collections.OrderedDict)):
    raise ValueError("Joint distribution with unordered variables can't "
                     "take positional args (saw {}).".format(args))

  value = model_unflatten_fn(kwargs[k] if k in kwargs else args[i]
                             for i, k in enumerate(flat_names))
  return value, unmatched_kwargs


def maybe_check_wont_broadcast(flat_xs, validate_args):
  """Verifies that `parts` don't broadcast."""
  flat_xs = tuple(flat_xs)  # So we can receive generators.
  if not validate_args:
    # Note: we don't try static validation because it is theoretically
    # possible that a user wants to take advantage of broadcasting.
    # Only when `validate_args` is `True` do we enforce the validation.
    return flat_xs
  msg = 'Broadcasting probably indicates an error in model specification.'
  s = tuple(prefer_static.shape(x) for x in flat_xs)
  if all(prefer_static.is_numpy(s_) for s_ in s):
    if not all(np.all(a == b) for a, b in zip(s[1:], s[:-1])):
      raise ValueError(msg)
    return flat_xs
  assertions = [assert_util.assert_equal(a, b, message=msg)
                for a, b in zip(s[1:], s[:-1])]
  with tf.control_dependencies(assertions):
    return tuple(tf.identity(x) for x in flat_xs)


class _DefaultJointBijector(bijector_lib.Bijector):
  """Minimally-viable event space bijector for `JointDistribution`."""

  # TODO(b/148485798): Support joint bijectors in TransformedDistribution.
  def __init__(self, jd):
    with tf.name_scope('default_joint_bijector') as name:
      super(_DefaultJointBijector, self).__init__(
          forward_min_event_ndims=0,  # Dummy value, unused.
          validate_args=jd.validate_args,
          name=name)
      self._jd = jd

  def _check_inputs_not_none(self, value):
    if any(x is None for x in tf.nest.flatten(value)):
      raise ValueError('No `value` part can be `None`; saw: {}.'.format(value))

  # pylint: disable=protected-access
  def _evaluate_bijector(self, bijector_fn, values):
    gen = self._jd._model_coroutine()
    outputs = []
    d = next(gen)
    index = 0
    try:
      while True:
        dist = d.distribution if type(d).__name__ == 'Root' else d
        bijector = dist._experimental_default_event_space_bijector()

        # For discrete distributions, the default event space bijector is None.
        # For a joint distribution's discrete components, we want the behavior
        # of the Identity bijector.
        bijector = (identity_bijector.Identity()
                    if bijector is None else bijector)

        out, y = bijector_fn(bijector, values[index])
        outputs.append(out)
        d = gen.send(y)
        index += 1
    except StopIteration:
      pass
    return outputs

  def _event_shapes(self, input_shapes, event_shape_attr):
    """For forward/inverse static event shapes."""
    input_shapes = self._jd._model_flatten(input_shapes)
    support_bijectors = [
        d._experimental_default_event_space_bijector()
        for d in self._jd._get_single_sample_distributions()]
    output_shapes = [
        getattr(bijector, event_shape_attr)(input_shape)
        for (bijector, input_shape) in zip(support_bijectors, input_shapes)]
    return self._jd._model_unflatten(output_shapes)

  def forward(self, values, name=None):
    with tf.name_scope(name or 'forward'):
      values = self._jd._model_flatten(values)
      self._check_inputs_not_none(values)

      def bijector_fn(bijector, value):
        y = bijector.forward(value)
        return y, y

      out = self._evaluate_bijector(bijector_fn, values)
      return self._jd._model_unflatten(out)

  def inverse(self, values, name=None):
    with tf.name_scope(name or 'inverse'):
      self._check_inputs_not_none(values)
      values = self._jd._model_flatten(values)

      def bijector_fn(bijector, value):
        x = bijector.inverse(value)
        return x, value

      out = self._evaluate_bijector(bijector_fn, values)
      return self._jd._model_unflatten(out)

  def forward_log_det_jacobian(self, values, event_ndims, name=None):
    with tf.name_scope(name or 'forward_log_det_jacobian'):
      self._check_inputs_not_none(values)
      values = self._jd._model_flatten(values)
      event_ndims = self._jd._model_flatten(event_ndims)

      def bijector_fn(bijector, value):
        x, event_ndims = value
        y = bijector.forward(x)
        fldj = bijector.forward_log_det_jacobian(x, event_ndims)
        return fldj, y

      fldjs = self._evaluate_bijector(bijector_fn,
                                      list(zip(values, event_ndims)))
      return sum(fldjs)

  def inverse_log_det_jacobian(self, values, event_ndims, name=None):
    with tf.name_scope(name or 'inverse_log_det_jacobian'):
      self._check_inputs_not_none(values)
      values = self._jd._model_flatten(values)
      event_ndims = self._jd._model_flatten(event_ndims)

      def bijector_fn(bijector, value):
        y, event_ndims = value
        ildj = bijector.inverse_log_det_jacobian(y, event_ndims)
        return ildj, y

      ildjs = self._evaluate_bijector(bijector_fn,
                                      list(zip(values, event_ndims)))
      return sum(ildjs)
  # pylint: enable=protected-access

  # TODO(b/148485931): Fix bijector caching.
  def forward_event_shape(self, input_shapes):
    return self._event_shapes(input_shapes, 'forward_event_shape')

  def forward_event_shape_tensor(self, input_shapes, name=None):
    with tf.name_scope(name or 'forward_event_shape_tensor'):
      self._check_inputs_not_none(input_shapes)
      return self._event_shapes(input_shapes, 'forward_event_shape_tensor')

  def inverse_event_shape(self, output_shapes):
    return self._event_shapes(output_shapes, 'inverse_event_shape')

  def inverse_event_shape_tensor(self, output_shapes, name=None):
    with tf.name_scope('inverse_event_shape_tensor'):
      self._check_inputs_not_none(output_shapes)
      return self._event_shapes(output_shapes, 'inverse_event_shape_tensor')


