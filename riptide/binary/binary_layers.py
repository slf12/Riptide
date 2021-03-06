import tensorflow as tf
import tensorflow.keras as keras
from .binary_funcs import *
from functools import partial
from tensorflow.python.keras import backend as K
from tensorflow.python.keras import constraints
from tensorflow.python.keras import initializers
from tensorflow.python.keras import regularizers
from tensorflow.python.framework import common_shapes
from tensorflow.python.keras.engine.base_layer import InputSpec
from tensorflow.python.keras.engine.base_layer import Layer
from tensorflow.python.keras.utils import tf_utils
from tensorflow.python.training import distribution_strategy_context
from tensorflow.python.util.tf_export import tf_export
from tensorflow.keras.layers import GlobalAveragePooling2D, Flatten, Activation, PReLU, Input, Concatenate
"""Quantization scope, defines the modification of operator"""


class Config(object):
    """Configuration scope of current mode.

    This is used to easily switch between different
    model structure variants by simply calling into these functions.

    Parameters
    ----------
    actQ : function
        Activation quantization

    weightQ : function: name->function
        Maps name to quantize function.

    bits : Tensor
        When using HWGQ binarization, these are the possible values
        that can be used in approximation. For other binarization schemes,
        this should be the number of bits to use.

    use_bn: bool
        Whether to apply batch normalization at the end of the layer or not.

    use_qadd: bool
        If true, do quantization before addition in Qadd layers.

    Example
    -------
    import qnn

    with qnn.Config(actQ=qnn.quantize(bits=8, scale=8, signed=True),
                    weightQ=qnn.identity,
                    use_bn=True):
        net = qnn.get_model(model_name, **kwargs)
    """
    current = None

    def __init__(self,
                 actQ=None,
                 weightQ=None,
                 bits=None,
                 use_bn=True,
                 use_maxpool=True,
                 use_act=True,
                 bipolar=False,
                 shiftnorm_scale=1.0,
                 use_qadd=False):
        if actQ is not None:
            actQ = partial(actQ, bipolar=bipolar)
        self.actQ = actQ if actQ else lambda x: x
        self.weightQ = weightQ if weightQ else lambda x: x
        self.bits = bits
        self.use_bn = use_bn
        self.use_act = use_act
        self.bipolar = bipolar
        self.shiftnorm_scale = shiftnorm_scale
        self.use_qadd = use_qadd
        self.use_maxpool = use_maxpool

    def __enter__(self):
        self._old_manager = Config.current
        Config.current = self
        return self

    def __exit__(self, ptype, value, trace):
        Config.current = self._old_manager


class BinaryConv2D(keras.layers.Conv2D):
    def __init__(self, *args, **kwargs):
        super(BinaryConv2D, self).__init__(*args, **kwargs)
        self.scope = Config.current
        self.actQ = self.scope.actQ
        self.weightQ = self.scope.weightQ
        self.bits = self.scope.bits
        self.use_act = self.scope.use_act
        if self.use_act and self.activation is None:
            self.activation = Activation('relu')
        self.bipolar = self.scope.bipolar

    def call(self, inputs):
        with tf.name_scope("actQ"):
            tf.compat.v1.summary.histogram('prebinary_activations', inputs)
            if self.bits is not None:
                inputs = self.actQ(inputs, float(self.bits))
            else:
                inputs = self.actQ(inputs)
            tf.compat.v1.summary.histogram('binary_activations', inputs)
        with tf.name_scope("weightQ"):
            kernel = self.weightQ(self.kernel)
            tf.compat.v1.summary.histogram('weights', self.kernel)
            tf.compat.v1.summary.histogram('binary_weights', kernel)

        # If bipolar quantization is used, pad with -1 instead of 0.
        if self.bipolar:
            inputs = inputs + 1.0

        outputs = self._convolution_op(inputs, kernel)

        if self.bipolar:
            readjust_val = -1.0 * tf.reduce_sum(kernel, axis=[0, 1, 2])
            outputs = outputs + readjust_val

        if self.use_bias:
            if self.data_format == 'channels_first':
                outputs = tf.nn.bias_add(
                    outputs, self.bias, data_format='NCHW')
            else:
                outputs = tf.nn.bias_add(
                    outputs, self.bias, data_format='NHWC')

        if self.use_act and self.activation is not None:
            outputs = self.activation(outputs)

        return outputs


class BinaryDense(keras.layers.Dense):
    def __init__(self, *args, **kwargs):
        super(BinaryDense, self).__init__(*args, **kwargs)
        self.scope = Config.current
        self.actQ = self.scope.actQ
        self.weightQ = self.scope.weightQ
        self.bits = self.scope.bits
        self.use_act = self.scope.use_act
        if self.use_act and self.activation is None:
            self.activation = Activation('relu')

    def call(self, inputs):
        inputs = tf.convert_to_tensor(inputs, dtype=self.dtype)
        with tf.name_scope("actQ"):
            tf.compat.v1.summary.histogram('prebinary_activations', inputs)
            if self.bits is not None:
                inputs = self.actQ(inputs, float(self.bits))
            else:
                inputs = self.actQ(inputs)
            tf.compat.v1.summary.histogram('binary_activations', inputs)
        with tf.name_scope("weightQ"):
            kernel = self.weightQ(self.kernel)
            tf.compat.v1.summary.histogram('weights', self.kernel)
            tf.compat.v1.summary.histogram('binary_weights', kernel)
        rank = common_shapes.rank(inputs)
        if rank > 2:
            # Broadcasting is required for the inputs.
            outputs = tf.tensordot(inputs, kernel, [[rank - 1], [0]])
            # Reshape the output back to the original ndim of the input.
            if not context.executing_eagerly():
                shape = inputs.get_shape().as_list()
                output_shape = shape[:-1] + [self.units]
                outputs.set_shape(output_shape)
        else:
            outputs = tf.matmul(inputs, kernel)

        if self.use_bias:
            outputs = tf.nn.bias_add(outputs, self.bias)

        if self.use_act and self.activation is not None:
            outputs = self.activation(outputs)  # pylint: disable=not-callable

        return outputs


class Scalu(keras.layers.Layer):
    def __init__(self, scale=0.001):
        super(Scalu, self).__init__()
        self.scale = scale

    def build(self, input_shape):
        self.scale = self.add_variable(
            'scale',
            shape=[1],
            initializer=tf.keras.initializers.Constant(value=self.scale))

    def call(self, input):
        return input * self.scale


class QAdd(keras.layers.Layer):
    def __init__(self):
        super(QAdd, self).__init__()
        self.scope = Config.current
        self.bits = self.scope.bits
        self.act = self.scope.actQ
        self.use_q = self.scope.use_qadd

    def build(self, input_shape):
        self.scale = self.add_variable('scale', shape=[1], initializer='ones')

    def call(self, inputs):
        x = inputs[0]
        y = inputs[1]
        if self.use_q:
            x = self.act(x, self.bits)
            y = self.act(y, self.bits)
            with tf.name_scope("AP2"):
                approx_scale = AP2(self.scale)
            output = approx_scale * (x + y)
        else:
            output = x + y
        return output


class EnterInteger(keras.layers.Layer):
    def __init__(self, scale):
        super(EnterInteger, self).__init__()
        self.scale = scale
        self.scope = Config.current
        self.bits = self.scope.bits
        self.quantize = self.scope.bits != None

    def call(self, inputs):
        return self.scale * inputs

class ExitInteger(keras.layers.Layer):
    def call(self, inputs):
        return inputs


class SpecialBatchNormalization(keras.layers.BatchNormalization):
    def __init__(self, *args, **kwargs):
        super(SpecialBatchNormalization, self).__init__(**kwargs)

    def call(self, inputs, training=None):
        return super(SpecialBatchNormalization, self).call(inputs, training)


class ResidualConnect(Layer):
    def __init__(self, *args, **kwargs):
        super(ResidualConnect, self).__init__()

    def call(self, inputs):
        return tf.concat(inputs, axis=-1)


def BatchNormalization(*args, **kwargs):
    scope = Config.current
    if scope.use_bn:
        return SpecialBatchNormalization(*args, **kwargs)
    else:
        return ShiftNormalization(*args, **kwargs)

class UnfusedBatchNorm(keras.layers.BatchNormalization):
    def __init__(self, *args, **kwargs):
        super(UnfusedBatchNorm, self).__init__(*args, **kwargs)


def MaxPool2D(*args, **kwargs):
    scope = Config.current
    if scope.use_maxpool:
        return keras.layers.MaxPool2D(*args, **kwargs)
    else:
        return lambda x: x


class ShiftNormalization(Layer):
    """Shift normalization layer

  Normalize the activations of the previous layer at each batch,
  i.e. applies a transformation that maintains the mean activation
  close to 0 and the activation standard deviation close to 1.

  Arguments:
    axis: Integer, the axis that should be normalized
        (typically the features axis).
        For instance, after a `Conv2D` layer with
        `data_format="channels_first"`,
        set `axis=1` in `BatchNormalization`.
    binary_dense: Set true when using after a binary dense layer.
        Need some special handling for batchnorm for binary dense layers to
        prevent small variance.
    momentum: Momentum for the moving average.
    epsilon: Small float added to variance to avoid dividing by zero.
    center: If True, add offset of `beta` to normalized tensor.
        If False, `beta` is ignored.
    scale: If True, multiply by `gamma`.
        If False, `gamma` is not used.
        When the next layer is linear (also e.g. `nn.relu`),
        this can be disabled since the scaling
        will be done by the next layer.
    beta_initializer: Initializer for the beta weight.
    gamma_initializer: Initializer for the gamma weight.
    moving_mean_initializer: Initializer for the moving mean.
    moving_variance_initializer: Initializer for the moving variance.
    beta_regularizer: Optional regularizer for the beta weight.
    gamma_regularizer: Optional regularizer for the gamma weight.
    beta_constraint: Optional constraint for the beta weight.
    gamma_constraint: Optional constraint for the gamma weight.
    renorm: Whether to use Batch Renormalization
      (https://arxiv.org/abs/1702.03275). This adds extra variables during
      training. The inference is the same for either value of this parameter.
    renorm_clipping: A dictionary that may map keys 'rmax', 'rmin', 'dmax' to
      scalar `Tensors` used to clip the renorm correction. The correction
      `(r, d)` is used as `corrected_value = normalized_value * r + d`, with
      `r` clipped to [rmin, rmax], and `d` to [-dmax, dmax]. Missing rmax, rmin,
      dmax are set to inf, 0, inf, respectively.
    renorm_momentum: Momentum used to update the moving means and standard
      deviations with renorm. Unlike `momentum`, this affects training
      and should be neither too small (which would add noise) nor too large
      (which would give stale estimates). Note that `momentum` is still applied
      to get the means and variances for inference.
    trainable: Boolean, if `True` also add variables to the graph collection
      `GraphKeys.TRAINABLE_VARIABLES` (see tf.Variable).

  Input shape:
      Arbitrary. Use the keyword argument `input_shape`
      (tuple of integers, does not include the samples axis)
      when using this layer as the first layer in a model.

  Output shape:
      Same shape as input.

  References:
      - [Batch Normalization: Accelerating Deep Network Training by Reducing
        Internal Covariate Shift](https://arxiv.org/abs/1502.03167)
  """

    def __init__(self,
                 previous_layer,
                 axis=-1,
                 momentum=0.99,
                 epsilon=1e-3,
                 center=False,
                 scale=False,
                 beta_initializer='zeros',
                 gamma_initializer='ones',
                 moving_mean_initializer='zeros',
                 moving_variance_initializer='ones',
                 beta_regularizer=None,
                 gamma_regularizer=None,
                 beta_constraint=None,
                 gamma_constraint=None,
                 renorm=False,
                 renorm_clipping=None,
                 renorm_momentum=0.99,
                 trainable=True,
                 name=None,
                 **kwargs):
        super(ShiftNormalization, self).__init__(
            name=name, trainable=trainable, **kwargs)
        self.scope = Config.current
        self.bits = self.scope.bits
        self.previous_layer = previous_layer
        if isinstance(axis, list):
            self.axis = axis[:]
        else:
            self.axis = axis
        self.binary_dense = isinstance(previous_layer, BinaryDense)
        self.momentum = momentum
        self.epsilon = epsilon
        self.center = center
        self.scale = scale
        self.extra_scale = self.scope.shiftnorm_scale
        self.beta_initializer = initializers.get(beta_initializer)
        self.gamma_initializer = initializers.get(gamma_initializer)
        self.moving_mean_initializer = initializers.get(
            moving_mean_initializer)
        self.moving_variance_initializer = initializers.get(
            moving_variance_initializer)
        self.beta_regularizer = regularizers.get(beta_regularizer)
        self.gamma_regularizer = regularizers.get(gamma_regularizer)
        self.beta_constraint = constraints.get(beta_constraint)
        self.gamma_constraint = constraints.get(gamma_constraint)
        self.renorm = renorm
        self.supports_masking = True

        self._bessels_correction_test_only = True

        if renorm:
            renorm_clipping = renorm_clipping or {}
            keys = ['rmax', 'rmin', 'dmax']
            if set(renorm_clipping) - set(keys):
                raise ValueError('renorm_clipping %s contains keys not in %s' %
                                 (renorm_clipping, keys))
            self.renorm_clipping = renorm_clipping
            self.renorm_momentum = renorm_momentum

    def build(self, input_shape):
        input_shape = tf.TensorShape(input_shape)
        if not input_shape.ndims:
            raise ValueError('Input has undefined rank:', input_shape)
        ndims = len(input_shape)

        # Convert axis to list and resolve negatives
        if isinstance(self.axis, int):
            self.axis = [self.axis]

        if not isinstance(self.axis, list):
            raise TypeError(
                'axis must be int or list, type given: %s' % type(self.axis))

        for idx, x in enumerate(self.axis):
            if x < 0:
                self.axis[idx] = ndims + x

        # Validate axes
        for x in self.axis:
            if x < 0 or x >= ndims:
                raise ValueError('Invalid axis: %d' % x)
        if len(self.axis) != len(set(self.axis)):
            raise ValueError('Duplicate axis: %s' % self.axis)

        # Raise parameters of fp16 batch norm to fp32
        if self.dtype == tf.float16 or self.dtype == tf.bfloat16:
            param_dtype = tf.float32
        else:
            param_dtype = self.dtype or tf.float32

        axis_to_dim = {x: input_shape[x] for x in self.axis}
        for x in axis_to_dim:
            if axis_to_dim[x] is None:
                raise ValueError(
                    'Input has undefined `axis` dimension. Input shape: ',
                    input_shape)
        self.input_spec = InputSpec(ndim=ndims, axes=axis_to_dim)

        if self.binary_dense:
            param_shape = [1]
        elif len(axis_to_dim) == 1:
            # Single axis batch norm (most common/default use-case)
            param_shape = (list(axis_to_dim.values())[0], )
        else:
            # Parameter shape is the original shape but with 1 in all non-axis dims
            param_shape = [
                axis_to_dim[i] if i in axis_to_dim else 1 for i in range(ndims)
            ]

        if self.scale:
            self.gamma = self.add_weight(
                name='gamma',
                shape=param_shape,
                dtype=param_dtype,
                initializer=self.gamma_initializer,
                regularizer=self.gamma_regularizer,
                constraint=self.gamma_constraint,
                trainable=True)
        else:
            self.gamma = None

        if self.center:
            self.beta = self.add_weight(
                name='beta',
                shape=param_shape,
                dtype=param_dtype,
                initializer=self.beta_initializer,
                regularizer=self.beta_regularizer,
                constraint=self.beta_constraint,
                trainable=True)
        else:
            self.beta = None

        try:
            # Disable variable partitioning when creating the moving mean and variance
            if hasattr(self, '_scope') and self._scope:
                partitioner = self._scope.partitioner
                self._scope.set_partitioner(None)
            else:
                partitioner = None
            self.moving_mean = self.add_weight(
                name='moving_mean',
                shape=param_shape,
                dtype=param_dtype,
                initializer=self.moving_mean_initializer,
                synchronization=tf.VariableSynchronization.ON_READ,
                trainable=False,
                aggregation=tf.VariableAggregation.MEAN)

            self.moving_variance = self.add_weight(
                name='moving_variance',
                shape=param_shape,
                dtype=param_dtype,
                initializer=self.moving_variance_initializer,
                synchronization=tf.VariableSynchronization.ON_READ,
                trainable=False,
                aggregation=tf.VariableAggregation.MEAN)

            if self.renorm:
                # Create variables to maintain the moving mean and standard deviation.
                # These are used in training and thus are different from the moving
                # averages above. The renorm variables are colocated with moving_mean
                # and moving_variance.
                # NOTE: below, the outer `with device` block causes the current device
                # stack to be cleared. The nested ones use a `lambda` to set the desired
                # device and ignore any devices that may be set by the custom getter.
                def _renorm_variable(name, shape):
                    var = self.add_weight(
                        name=name,
                        shape=shape,
                        dtype=param_dtype,
                        initializer=tf.zeros_initializer(),
                        synchronization=tf.VariableSynchronization.ON_READ,
                        trainable=False,
                        aggregation=tf.VariableAggregation.MEAN)
                    return var

                with distribution_strategy_context.get_distribution_strategy(
                ).colocate_vars_with(self.moving_mean):
                    self.renorm_mean = _renorm_variable(
                        'renorm_mean', param_shape)
                    self.renorm_mean_weight = _renorm_variable(
                        'renorm_mean_weight', ())
                # We initialize renorm_stddev to 0, and maintain the (0-initialized)
                # renorm_stddev_weight. This allows us to (1) mix the average
                # stddev with the minibatch stddev early in training, and (2) compute
                # the unbiased average stddev by dividing renorm_stddev by the weight.
                with distribution_strategy_context.get_distribution_strategy(
                ).colocate_vars_with(self.moving_variance):
                    self.renorm_stddev = _renorm_variable(
                        'renorm_stddev', param_shape)
                    self.renorm_stddev_weight = _renorm_variable(
                        'renorm_stddev_weight', ())
        finally:
            if partitioner:
                self._scope.set_partitioner(partitioner)
        self.built = True

    def _assign_moving_average(self, variable, value, momentum):
        with tf.compat.v1.name_scope(None, 'AssignMovingAvg',
                           [variable, value, momentum]) as scope:
            with tf.compat.v1.colocate_with(variable):
                decay = tf.convert_to_tensor(1.0 - momentum, name='decay')
                if decay.dtype != variable.dtype.base_dtype:
                    decay = tf.cast(decay, variable.dtype.base_dtype)
                update_delta = (
                    variable - tf.cast(value, variable.dtype)) * decay
                return tf.compat.v1.assign_sub(variable, update_delta, name=scope)

    def _renorm_correction_and_moments(self, mean, variance, training):
        """Returns the correction and update values for renorm."""
        stddev = tf.sqrt(variance + self.epsilon)
        # Compute the average mean and standard deviation, as if they were
        # initialized with this batch's moments.
        mixed_renorm_mean = (self.renorm_mean +
                             (1. - self.renorm_mean_weight) * mean)
        mixed_renorm_stddev = (self.renorm_stddev +
                               (1. - self.renorm_stddev_weight) * stddev)
        # Compute the corrections for batch renorm.
        r = stddev / mixed_renorm_stddev
        d = (mean - mixed_renorm_mean) / mixed_renorm_stddev
        # Ensure the corrections use pre-update moving averages.
        with tf.control_dependencies([r, d]):
            mean = tf.identity(mean)
            stddev = tf.identity(stddev)
        rmin, rmax, dmax = [
            self.renorm_clipping.get(key) for key in ['rmin', 'rmax', 'dmax']
        ]
        if rmin is not None:
            r = tf.maximum(r, rmin)
        if rmax is not None:
            r = tf.minimum(r, rmax)
        if dmax is not None:
            d = tf.maximum(d, -dmax)
            d = tf.minimum(d, dmax)
        # When not training, use r=1, d=0.
        r = tf_utils.smart_cond(training, lambda: r, lambda: tf.ones_like(r))
        d = tf_utils.smart_cond(training, lambda: d, lambda: tf.zeros_like(d))

        def _update_renorm_variable(var, weight, value):
            """Updates a moving average and weight, returns the unbiased value."""
            value = tf.identity(value)

            def _do_update():
                """Updates the var and weight, returns their updated ratio."""
                # Update the variables without zero debiasing. The debiasing will be
                # accomplished by dividing the exponential moving average by the weight.
                # For example, after a single update, the moving average would be
                # (1-decay) * value. and the weight will be 1-decay, with their ratio
                # giving the value.
                # Make sure the weight is not updated until before r and d computation.
                with tf.control_dependencies([value]):
                    weight_value = tf.constant(1., dtype=weight.dtype)
                new_var = self._assign_moving_average(var, value,
                                                      self.renorm_momentum)
                new_weight = self._assign_moving_average(
                    weight, weight_value, self.renorm_momentum)
                # TODO(yuefengz): the updates to var and weighted can not be batched
                # together if we fetch their updated values here. Consider calculating
                # new values and delaying the updates.
                return new_var / new_weight

            def _fake_update():
                return tf.identity(var)

            return tf_utils.smart_cond(training, _do_update, _fake_update)

        # TODO(yuefengz): colocate the operations
        new_mean = _update_renorm_variable(self.renorm_mean,
                                           self.renorm_mean_weight, mean)
        new_stddev = _update_renorm_variable(self.renorm_stddev,
                                             self.renorm_stddev_weight, stddev)
        # Make sqrt(moving_variance + epsilon) = new_stddev.
        new_variance = tf.square(new_stddev) - self.epsilon

        return (r, d, new_mean, new_variance)

    def call(self, inputs, training=None):
        # Extract weights of previous layer to compute proper scale.
        previous_weights = self.previous_layer.weights[0].value()
        original_training_value = training
        if training is None:
            training = K.learning_phase()

        in_eager_mode = tf.executing_eagerly()

        # Compute the axes along which to reduce the mean / variance
        input_shape = inputs.get_shape()
        ndims = len(input_shape)
        # For dense layers, require a full reduction.
        if self.binary_dense:
            reduction_axes = [i for i in range(ndims)]
        # Otherwise, reduce all but the feature axis.
        else:
            reduction_axes = [i for i in range(ndims) if i not in self.axis]

        # Broadcasting only necessary for single-axis batch norm where the axis is
        # not the last dimension
        broadcast_shape = [1] * ndims
        broadcast_shape[self.axis[0]] = input_shape[self.axis[0]]

        def _broadcast(v):
            if (v is not None and len(v.get_shape()) != ndims
                    and reduction_axes != list(range(ndims - 1))):
                return tf.reshape(v, broadcast_shape)
            return v

        scale, offset = _broadcast(self.gamma), _broadcast(self.beta)

        def _compose_transforms(scale, offset, then_scale, then_offset):
            if then_scale is not None:
                scale *= then_scale
                offset *= then_scale
            if then_offset is not None:
                offset += then_offset
            return (scale, offset)

        # Determine a boolean value for `training`: could be True, False, or None.
        training_value = tf_utils.constant_value(training)
        if training_value is not False:
            # Some of the computations here are not necessary when training==False
            # but not a constant. However, this makes the code simpler.
            keep_dims = len(self.axis) > 1

            mean, variance = tf.compat.v1.nn.moments(
                inputs, reduction_axes, keep_dims=keep_dims)

            # When norming the output of a binary dense layer,
            # need to make sure shape is maintained.
            if self.binary_dense:
                mean = tf.reshape(mean, [1])
                variance = tf.reshape(variance, [1])

            moving_mean = self.moving_mean
            moving_variance = self.moving_variance

            mean = tf_utils.smart_cond(training, lambda: mean,
                                       lambda: moving_mean)
            variance = tf_utils.smart_cond(training, lambda: variance,
                                           lambda: moving_variance)

            new_mean, new_variance = mean, variance

            if self.renorm:
                r, d, new_mean, new_variance = self._renorm_correction_and_moments(
                    new_mean, new_variance, training)
                # When training, the normalized values (say, x) will be transformed as
                # x * gamma + beta without renorm, and (x * r + d) * gamma + beta
                # = x * (r * gamma) + (d * gamma + beta) with renorm.
                r = _broadcast(tf.stop_gradient(r, name='renorm_r'))
                d = _broadcast(tf.stop_gradient(d, name='renorm_d'))
                scale, offset = _compose_transforms(r, d, scale, offset)

            def _do_update(var, value):
                if in_eager_mode and not self.trainable:
                    return

                return self._assign_moving_average(var, value, self.momentum)

            mean_update = tf_utils.smart_cond(
                training, lambda: _do_update(self.moving_mean, new_mean),
                lambda: self.moving_mean)
            variance_update = tf_utils.smart_cond(
                training,
                lambda: _do_update(self.moving_variance, new_variance),
                lambda: self.moving_variance)
            if not tf.executing_eagerly():
                self.add_update(mean_update, inputs=True)
                self.add_update(variance_update, inputs=True)

        else:
            mean, variance = self.moving_mean, self.moving_variance

        mean = tf.cast(mean, inputs.dtype)
        variance = tf.cast(variance, inputs.dtype)
        if offset is not None:
            offset = tf.cast(offset, inputs.dtype)
        #outputs = nn.batch_normalization(inputs, _broadcast(mean),
        #                                 _broadcast(variance), offset, scale,
        #                                 self.epsilon)

        approximate_std, quantized_means = compute_quantized_shiftnorm(
            variance,
            mean,
            self.epsilon,
            previous_weights,
            self.extra_scale,
            self.bits,
            rescale=True)

        outputs = inputs - quantized_means
        outputs = outputs * approximate_std

        if scale:
            outputs = scale * outputs
        if offset:
            outputs = outputs + offset

        # If some components of the shape got lost due to adjustments, fix that.
        outputs.set_shape(input_shape)

        if not tf.executing_eagerly() and original_training_value is None:
            outputs._uses_learning_phase = True  # pylint: disable=protected-access
        return outputs

    def compute_output_shape(self, input_shape):
        return input_shape

    def get_config(self):
        config = {
            'axis':
            self.axis,
            'momentum':
            self.momentum,
            'epsilon':
            self.epsilon,
            'center':
            self.center,
            'scale':
            self.scale,
            'beta_initializer':
            initializers.serialize(self.beta_initializer),
            'gamma_initializer':
            initializers.serialize(self.gamma_initializer),
            'moving_mean_initializer':
            initializers.serialize(self.moving_mean_initializer),
            'moving_variance_initializer':
            initializers.serialize(self.moving_variance_initializer),
            'beta_regularizer':
            regularizers.serialize(self.beta_regularizer),
            'gamma_regularizer':
            regularizers.serialize(self.gamma_regularizer),
            'beta_constraint':
            constraints.serialize(self.beta_constraint),
            'gamma_constraint':
            constraints.serialize(self.gamma_constraint)
        }
        # Only add TensorFlow-specific parameters if they are set, so as to preserve
        # model compatibility with external Keras.
        if self.renorm:
            config['renorm'] = True
            config['renorm_clipping'] = self.renorm_clipping
            config['renorm_momentum'] = self.renorm_momentum
        base_config = super(ShiftNormalization, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


NormalDense = keras.layers.Dense
NormalConv2D = keras.layers.Conv2D
NormalMaxPool2D = keras.layers.MaxPool2D
NormalBatchNormalization = keras.layers.BatchNormalization
