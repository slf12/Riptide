import os
import tensorflow as tf
import tensorflow.keras.layers as nn

class vggnet(tf.keras.Model):
    def __init__(self, classes=1000):
        super(vggnet, self).__init__()

        self.conv1 = nn.Conv2D(
            filters=96,
            kernel_size=7,
            strides=2,
            padding='same',
            activation=None,
            use_bias=False)
        self.pool1 = nn.MaxPool2D(pool_size=2, strides=2)
        self.bn1 = nn.BatchNormalization(center=False, scale=False)

        self.conv2 = nn.Conv2D(
            filters=256,
            kernel_size=3,
            strides=1,
            padding='same',
            activation='relu',
            use_bias=False)
        self.bn2 = nn.BatchNormalization()

        self.conv3 = nn.Conv2D(
            filters=256,
            kernel_size=3,
            strides=1,
            padding='same',
            activation='relu',
            use_bias=False)
        self.bn3 = nn.BatchNormalization()

        self.conv4 = nn.Conv2D(
            filters=256,
            kernel_size=3,
            strides=1,
            padding='same',
            activation='relu',
            use_bias=False)
        self.pool4 = nn.MaxPool2D(pool_size=2, strides=2)
        self.bn4 = nn.BatchNormalization()

        self.conv5 = nn.Conv2D(
            filters=512,
            kernel_size=3,
            strides=1,
            padding='same',
            activation='relu',
            use_bias=False)
        self.bn5 = nn.BatchNormalization()

        self.conv6 = nn.Conv2D(
            filters=512,
            kernel_size=3,
            strides=1,
            padding='same',
            activation='relu',
            use_bias=False)
        self.bn6 = nn.BatchNormalization()

        self.conv7 = nn.Conv2D(
            filters=512,
            kernel_size=3,
            strides=1,
            padding='same',
            activation='relu',
            use_bias=False)
        self.pool7 = nn.MaxPool2D(pool_size=2, strides=2)
        self.bn7 = nn.BatchNormalization()

        self.conv8 = nn.Conv2D(
            filters=512,
            kernel_size=3,
            strides=1,
            padding='same',
            activation='relu',
            use_bias=False)
        self.bn8 = nn.BatchNormalization()

        self.conv9 = nn.Conv2D(
            filters=512,
            kernel_size=3,
            strides=1,
            padding='same',
            activation='relu',
            use_bias=False)
        self.bn9 = nn.BatchNormalization()

        self.conv10 = nn.Conv2D(
            filters=512,
            kernel_size=3,
            strides=1,
            padding='same',
            activation='relu',
            use_bias=False)
        self.pool10 = nn.MaxPool2D(pool_size=2, strides=2)
        self.bn10 = nn.BatchNormalization()
        self.flatten = nn.Flatten()

        self.dense1 = nn.Dense(4096, use_bias=False, activation='relu')
        self.bn11 = nn.BatchNormalization()
        self.dense2 = nn.Dense(4096, use_bias=False, activation='relu')
        self.bn12 = nn.BatchNormalization()
        self.dense3 = nn.Dense(classes, use_bias=False)
        self.softmax = nn.Activation('softmax')

    def call(self, inputs, training=None, debug=False):
        layers = []
        with tf.name_scope('unbinarized'):
            x = self.conv1(inputs)
            layers.append(x)
            x = self.pool1(x)
            layers.append(x)
            x = self.bn1(x, training=training)
            layers.append(x)

        x = self.conv2(x)
        layers.append(x)
        x = self.bn2(
            x, training=training)
        layers.append(x)
        x = self.conv3(x)
        layers.append(x)
        x = self.bn3(
            x, training=training)
        layers.append(x)
        x = self.conv4(x)
        layers.append(x)
        x = self.pool4(x)
        layers.append(x)
        x = self.bn4(
            x, training=training)
        layers.append(x)

        x = self.conv5(x)
        layers.append(x)
        x = self.bn5(
            x, training=training)
        layers.append(x)
        x = self.conv6(x)
        layers.append(x)
        x = self.bn6(
            x, training=training)
        layers.append(x)
        x = self.conv7(x)
        layers.append(x)
        x = self.pool7(x)
        layers.append(x)
        x = self.bn7(
            x, training=training)
        layers.append(x)

        x = self.conv8(x)
        layers.append(x)
        x = self.bn8(
            x, training=training)
        layers.append(x)
        x = self.conv9(x)
        layers.append(x)
        x = self.bn9(
            x, training=training)
        layers.append(x)
        x = self.conv10(x)
        layers.append(x)
        x = self.pool10(x)
        layers.append(x)
        x = self.bn10(
            x, training=training)
        layers.append(x)
        x = self.flatten(x)
        layers.append(x)

        x = self.dense1(x)
        layers.append(x)
        x = self.bn11(
            x, training=training)
        layers.append(x)
        x = self.dense2(x)
        layers.append(x)
        x = self.bn12(
            x, training=training)
        layers.append(x)
        x = self.dense3(x)
        layers.append(x)
        x = self.softmax(x)
        tf.compat.v1.summary.histogram('output', x)

        if debug:
            return layers
        else:
            return x
