# imports
import os
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

import keras
from keras.models import Model
from keras.layers import Conv2D, LeakyReLU, Concatenate, Lambda, Dense, Add, Input, BatchNormalization, MaxPooling2D

from keras.optimizers import Adam

# own packages:
import data_io
import augmentation



# TRAINING VARIABLES
EPOCHS = 50
BATCH_SIZE = 10
IMAGES_PER_SPLIT = 2
SAMPLE_INTERVAL = 20
GENERATOR_EVOLUTION_DATA = []
GENERATOR_EVOLUTION_INDIZES = [1, 10, 20, 40]
GENERATED_DATA_LOCATION = 'generated_images/esrgan/'
DATASET_PATH = ''
MODEL_WEIGHTS_PATH = 'models/esrgan/'

# - - - - - - - - - -


class ESRGAN():

    def __init__(self):
        self.name_string = ''
        self.img_rows = 128
        self.img_cols = 128
        self.img_channels_condition = 3
        self.img_channels_target = 3
        self.img_shape_condition = (self.img_rows, self.img_cols, self.img_channels_condition)
        self.img_shape_target = (self.img_rows, self.img_cols, self.img_channels_target)

        self.num_f_g = 32
        self.num_f_d = 64   # TODO: 64
        self.f_size = 3
        self.num_rrdbs = 16  # TODO: 16

        self.generator = self.make_generator()
        # self.generator.summary()

        self.discriminator = self.make_discriminator()
        # self.discriminator.summary()
        self.discriminator_output_shape = list(self.discriminator.output_shape)
        self.discriminator_output_shape[0] = BATCH_SIZE
        self.discriminator_output_shape = tuple(self.discriminator_output_shape)

        self.vgg19 = self.make_vgg19(low_level_features_only=False)
        # self.vgg19.summary()
        self.vgg19.trainable = False

        # parameters to balance the loss function of the combined model:
        self.factor_perceptual = 0.1
        self.factor_adversarial = 1/0.005
        self.factor_l1 = 1/0.005

        self.lr_g = 0.0001
        self.lr_d = 0.0001

        self.opt_g = Adam(self.lr_g)
        self.opt_d = Adam(self.lr_d)

        # compile discriminator:
        self.discriminator.compile(optimizer=self.opt_d, loss='mse', metrics=['accuracy'])

        # create and compile combined model:
        self.discriminator.trainable = False
        img_opt = Input(shape=self.img_shape_condition)
        # img_sar = Input(shape=self.img_shape_target)
        img_fake = self.generator(img_opt)
        fake_features = self.vgg19(img_fake)
        validity = self.discriminator(img_fake)
        # self.combined = Model(inputs=[img_opt, img_sar], outputs=[validity, fake_features])           # TODO
        self.combined = Model(inputs=[img_opt], outputs=[validity, fake_features, img_fake])
        # self.combined.compile(optimizer=self.opt_g, loss=['binary_crossentropy', 'mse'], loss_weights=[1e-3, 1])
        self.combined.compile(optimizer=self.opt_g,
                              loss=['binary_crossentropy', 'mse', 'mae'],
                              loss_weights=[self.factor_adversarial, self.factor_perceptual, self.factor_l1])
        self.combined.summary()

    def make_generator(self):

        # no activation by default
        def conv_lrelu(inp, filters, f_size, use_act=False, alpha=0.0):
            outp = Conv2D(filters, kernel_size=f_size, strides=1, padding='same')(inp)
            if use_act:
                outp = LeakyReLU(alpha=alpha)(outp)
            return outp

        def dense_block(inp0, filters, f_size, alpha, residual_scaling_factor):
            # Densely Connected Convolutional Networks
            out1 = conv_lrelu(inp0, filters, f_size, use_act=True, alpha=alpha)
            inp = Concatenate(axis=-1)([inp0, out1])
            out2 = conv_lrelu(inp, filters, f_size, use_act=True, alpha=alpha)
            inp = Concatenate(axis=-1)([inp0, out1, out2])
            out3 = conv_lrelu(inp, filters, f_size, use_act=True, alpha=alpha)
            inp = Concatenate(axis=-1)([inp0, out1, out2, out3])
            out4 = conv_lrelu(inp, filters, f_size, use_act=True, alpha=alpha)
            inp = Concatenate(axis=-1)([inp0, out1, out2, out3, out4])
            out5 = conv_lrelu(inp, filters, f_size)

            out = Lambda(lambda x: x * residual_scaling_factor)(out5)
            return Add()([inp0, out])

        def rrd_block(inp, filters, f_size, alpha, residual_scaling_factor):
            outp = dense_block(inp, filters, f_size, alpha, residual_scaling_factor)
            outp = dense_block(outp, filters, f_size, alpha, residual_scaling_factor)
            outp = dense_block(outp, filters, f_size, alpha, residual_scaling_factor)
            outp = Lambda(lambda x: x * residual_scaling_factor)(outp)
            return Add()([inp, outp])

        inp = Input(shape=self.img_shape_condition)
        # first convolution (no activation function):
        outp = conv_lrelu(inp, filters=self.num_f_g, f_size=self.f_size*3)
        # save output of first convolution to add it to the output of the RRDB's:
        out1 = outp
        # RRDB's:
        for _ in range(self.num_rrdbs):
            outp = rrd_block(outp, filters=self.num_f_g, f_size=self.f_size, alpha=0.2, residual_scaling_factor=0.2)
        # convolution after RRDB's (no activation function):
        outp = conv_lrelu(outp, filters=self.num_f_g, f_size=self.f_size)
        # Add output of convolution before and after RRDB's:
        outp = Add()([out1, outp])
        # here upsampling would normally take place
        # ...
        # two convolutions at the end; the first with LeakyReLU, the second with tanh (not mentioned in paper)
        outp = conv_lrelu(outp, filters=self.num_f_g, f_size=self.f_size, use_act=True, alpha=0.2)
        # outp = conv_lrelu(outp, filters=self.img_channels_sar, f_size=self.f_size*3)
        outp = Conv2D(filters=self.img_channels_target, kernel_size=self.f_size * 3, activation='tanh', strides=1, padding='same')(outp)
        return Model(inp, outp, name='Generator')

    def make_discriminator(self):
        def conv_block(inp, filters, f_size, strides, alpha, use_bn=True):
            outp = Conv2D(filters, kernel_size=f_size, strides=strides, padding='same')(inp)
            if use_bn:
                outp = BatchNormalization()(outp)
            outp = LeakyReLU(alpha)(outp)
            return outp

        inp = Input(shape=self.img_shape_target)
        outp = conv_block(inp, self.num_f_d, self.f_size, strides=1, alpha=0.2, use_bn=False)
        outp = conv_block(outp, self.num_f_d, self.f_size, strides=2, alpha=0.2)
        outp = conv_block(outp, self.num_f_d * 2, self.f_size, strides=1, alpha=0.2)
        outp = conv_block(outp, self.num_f_d * 2, self.f_size, strides=2, alpha=0.2)
        outp = conv_block(outp, self.num_f_d * 4, self.f_size, strides=1, alpha=0.2)
        outp = conv_block(outp, self.num_f_d * 4, self.f_size, strides=2, alpha=0.2)
        outp = conv_block(outp, self.num_f_d * 8, self.f_size, strides=1, alpha=0.2)
        outp = conv_block(outp, self.num_f_d * 8, self.f_size, strides=2, alpha=0.2)
        # outp = Flatten()(outp)
        outp = Dense(1024)(outp)
        outp = LeakyReLU(0.2)(outp)
        outp = Dense(1, activation='sigmoid')(outp)
        return Model(inp, outp, name='Discriminator')

    def make_vgg19(self, low_level_features_only=False):
        vgg19_means = [103.939, 116.779, 123.68]

        vgg_inp = Input(shape=(self.img_rows, self.img_cols, 3))     # 3 channels are required for VGG19
        # Block 1
        x = Conv2D(64, (3, 3), activation='relu', padding='same', name='block1_conv1')(vgg_inp)
        x = Conv2D(64, (3, 3), activation='relu', padding='same', name='block1_conv2')(x)
        x = MaxPooling2D((2, 2), strides=(2, 2), name='block1_pool')(x)

        # Block 2
        x = Conv2D(128, (3, 3), activation='relu', padding='same', name='block2_conv1')(x)
        x = Conv2D(128, (3, 3), activation='relu', padding='same', name='block2_conv2')(x)
        x = MaxPooling2D((2, 2), strides=(2, 2), name='block2_pool')(x)

        # Block 3
        x = Conv2D(256, (3, 3), activation='relu', padding='same', name='block3_conv1')(x)
        x = Conv2D(256, (3, 3), activation='relu', padding='same', name='block3_conv2')(x)
        x = Conv2D(256, (3, 3), activation='relu', padding='same', name='block3_conv3')(x)
        if low_level_features_only:
            x = Conv2D(256, (3, 3), padding='same', name='block3_conv4')(x)
            # (LeakyReLu with alpha=0.0 is ReLU)
            x = LeakyReLU(alpha=0.0)(x)
            x = MaxPooling2D((2, 2), strides=(2, 2), name='block3_pool')(x)
        else:
            x = Conv2D(256, (3, 3), activation='relu', padding='same', name='block3_conv4')(x)
            x = MaxPooling2D((2, 2), strides=(2, 2), name='block3_pool')(x)

            # Block 4
            x = Conv2D(512, (3, 3), activation='relu', padding='same', name='block4_conv1')(x)
            x = Conv2D(512, (3, 3), activation='relu', padding='same', name='block4_conv2')(x)
            x = Conv2D(512, (3, 3), activation='relu', padding='same', name='block4_conv3')(x)
            x = Conv2D(512, (3, 3), activation='relu', padding='same', name='block4_conv4')(x)
            x = MaxPooling2D((2, 2), strides=(2, 2), name='block4_pool')(x)

            # Block 5
            x = Conv2D(512, (3, 3), activation='relu', padding='same', name='block5_conv1')(x)
            x = Conv2D(512, (3, 3), activation='relu', padding='same', name='block5_conv2')(x)
            x = Conv2D(512, (3, 3), activation='relu', padding='same', name='block5_conv3')(x)
            # change the definition here to access the values before the activation function is applied (compare ESRGAN)
            x = Conv2D(512, (3, 3), padding='same', name='block5_conv4')(x)
            # (LeakyReLu with alpha=0.0 is ReLU)
            x = LeakyReLU(alpha=0.0)(x)
            x = MaxPooling2D((2, 2), strides=(2, 2), name='block5_pool')(x)

        # Load the weights:
        vgg = Model(vgg_inp, x)
        WEIGHTS_PATH_NO_TOP = 'https://github.com/fchollet/deep-learning-models/releases/download/v0.1/vgg19_weights_tf_dim_ordering_tf_kernels_notop.h5'
        weights_path = keras.applications.vgg19.get_file('vgg19_weights_tf_dim_ordering_tf_kernels_notop.h5', WEIGHTS_PATH_NO_TOP, cache_subdir='models')
        if low_level_features_only:
            vgg.load_weights(weights_path, by_name=True)
            vgg.outputs = [vgg.layers[10].output]
        else:
            vgg.load_weights(weights_path, by_name=True)
            # set output of vgg model to output of last convolutional layer before activation function:
            vgg.outputs = [vgg.layers[20].output]

        # generate graph that is able to take 1D input instead of 3D:
        inp = Input(shape=self.img_shape_target)
        # normalize as expected by VGG19
        # (roughly; expected is mean subtraction; here we just subtract 127.5 from [0, 255] (input is in [-1, 1]))
        # rescale from [-1, 1] to [0, 255]
        outp = Lambda(lambda x: (x+1) * 127.5, name='rescale_to_255')(inp)
        if self.img_channels_target == 1:
            # transform rgb-->bgr and subtract means as done in the VGG19 paper:
            out3 = Lambda(lambda x: x - vgg19_means[0], name='sub_mean_B')(outp)
            out2 = Lambda(lambda x: x - vgg19_means[1], name='sub_mean_G')(outp)
            out1 = Lambda(lambda x: x - vgg19_means[2], name='sub_mean_R')(outp)
            outp = Concatenate(name='concat_with_rgb2bgr')([out3, out2, out1])
        elif self.img_channels_target == 3:
            # just a toy solution , one normally has to transform rgb-->bgr and then subtract each mean
            outp = Lambda(lambda x: x - 127.5)(outp)
        else:
            raise ValueError('VGG requires target channels to be either 1 or 3, you passed ' + str(self.img_channels_target))
        outp = vgg(outp)
        return Model(inp, outp, name='VGG19')

        # # return only the required part of the Model:
        # return Model(vgg_inp, vgg.layers[20].output, name='VGG19')

    def train_aerial(self):
        self.name_string = self.name_string + 'aerial'

        os.mkdir(GENERATED_DATA_LOCATION + self.name_string)

        print('--- Load datasets ...')
        aerial_train, map_train, aerial_test, map_test = data_io.load_dataset_maps('data/maps/ex_maps_small.hdf5')

        # smaller part of dataset:
        # aerial_train = aerial_train[:500, ...]
        # map_train = map_train[:500, ...]
        aerial_test = aerial_test[:100, ...]
        map_test = map_test[:100, ...]

        # cut images:
        print('--- divide images ...')
        aerial_train = augmentation.split_images(aerial_train, 2)
        print('sar_test done')
        map_train = augmentation.split_images(map_train, 2)
        print('opt_test done')
        aerial_test = augmentation.split_images(aerial_test, 2)
        print('sar_train done')
        map_test = augmentation.split_images(map_test, 2)
        print('opt_train done')

        # normalize datasets:
        print('--- normalize datasets ...')
        aerial_test = np.array(aerial_test / 127.5 - 1, dtype=np.float32)
        print('aerial_test done')
        map_test = np.array(map_test / 127.5 - 1, dtype=np.float32)
        print('map_test done')
        aerial_train = np.array(aerial_train / 127.5 - 1, dtype=np.float32)
        print('aerial_train done')
        map_train = np.array(map_train / 127.5 - 1, dtype=np.float32)
        print('map_train done')

        num_train = aerial_train.shape[0]
        num_test = aerial_test.shape[0]

        # discriminator targets:
        real = np.ones(self.discriminator_output_shape)
        fake = np.zeros(self.discriminator_output_shape)

        rep = 0
        for epoch in range(EPOCHS):
            # shuffle datasets:
            p = np.random.permutation(num_train)
            map_train = map_train[p]
            aerial_train = aerial_train[p]

            for batch_i in range(0, num_train, BATCH_SIZE):
                # get batch:
                imgs_cond = map_train[batch_i:batch_i+BATCH_SIZE]
                imgs_targ = aerial_train[batch_i:batch_i+BATCH_SIZE]

                imgs_gen = self.generator.predict(imgs_cond)

                num_samples = imgs_targ.shape[0]

                # train discriminator:
                real_loss = self.discriminator.train_on_batch(imgs_targ, real[:num_samples])
                fake_loss = self.discriminator.train_on_batch(imgs_gen, fake[:num_samples])
                d_loss = 0.5 * np.add(real_loss, fake_loss)

                # train generator:
                real_features = self.vgg19.predict(imgs_targ)
                # g_loss = self.combined.train_on_batch(x=[imgs_cond, imgs_targ], y=[real[:num_samples], real_features])      # TODO
                g_loss = self.combined.train_on_batch(x=[imgs_cond], y=[real[:num_samples], real_features, imgs_targ])

                # print to stdout:
                print_string = "[Epoch {:5d}/{:5d}, Batch {:4d}/{:4d}] \t "
                print_string += "[D loss: {:05.3f}, acc: {:05.2f}%] \t "
                print_string += "[G loss: {:05.2f},\t "
                print_string += "(adv.: {:04.2f} ({:04.2f}), perc.: {:05.2f} ({:04.2f}), l1: {:04.2f} ({:04.2f}))]"
                print(print_string.format(epoch + 1, EPOCHS, int(batch_i / BATCH_SIZE), int(num_train / BATCH_SIZE),
                                          d_loss[0], 100 * d_loss[1], g_loss[0],
                                          g_loss[1], g_loss[1]*self.factor_adversarial,
                                          g_loss[2], g_loss[2]*self.factor_perceptual,
                                          g_loss[3], g_loss[3]*self.factor_l1))

                if rep % SAMPLE_INTERVAL == 0:
                    i = np.random.randint(low=0, high=num_test, size=3)
                    img_batch = aerial_test[i], map_test[i]
                    # img_batch = map_test[i], aerial_test[i]
                    self.sample_images(epoch, rep, img_batch)
                    img_batch = aerial_test[GENERATOR_EVOLUTION_INDIZES], map_test[GENERATOR_EVOLUTION_INDIZES]
                    # img_batch = map_test[GENERATOR_EVOLUTION_INDIZES], aerial_test[GENERATOR_EVOLUTION_INDIZES]
                    self.generator_evolution(epoch, SAMPLE_INTERVAL, rep, img_batch)
                rep += 1
            self.save_generator(self.name_string)

    def generator_evolution(self, epoch, sample_interval, repetition, img_batch):

        imgs_gen_real, imgs_cond = img_batch
        imgs_gen = self.generator.predict(imgs_cond)

        imgs_gen = 0.5 * imgs_gen + 0.5
        GENERATOR_EVOLUTION_DATA.append(imgs_gen)
        # self.generator_evolution_data.append(imgs_gen)

        num_images_to_show = 5
        if repetition == 0:
            return
        if repetition % (sample_interval * num_images_to_show) == 0:
            imgs_gen_real = 0.5 * imgs_gen_real + 0.5
            imgs_cond = 0.5 * imgs_cond + 0.5
            fig, axs = plt.subplots(4, 7, figsize=(14, 8))
            for i in range(4):
                # plot condition image
                axs[i, 0].imshow(imgs_cond[i])
                axs[i, 0].set_title('Condition')
                axs[i, 0].axis('off')
                # plot generated images:
                for j in range(1, num_images_to_show + 1):
                    idx = int(j * repetition / (sample_interval * num_images_to_show))
                    if self.img_channels_target == 1:
                        axs[i, j].imshow(GENERATOR_EVOLUTION_DATA[idx][i, :, :, 0], cmap='gray')
                    else:
                        axs[i, j].imshow(GENERATOR_EVOLUTION_DATA[idx][i, ...])
                    axs[i, j].set_title(idx * sample_interval)
                    axs[i, j].axis('off')
                # plot original image:
                if self.img_channels_target == 1:
                    axs[i, 6].imshow(imgs_gen_real[i, :, :, 0], cmap='gray')
                else:
                    axs[i, 6].imshow(imgs_gen_real[i, ...])
                axs[i, 6].set_title('Original')
                axs[i, 6].axis('off')
            fig.savefig(GENERATED_DATA_LOCATION + self.name_string + '/' + 'evolution_{}_{}.png'.format(epoch+1, repetition))
            plt.close()

    def sample_images(self, epoch, repetition, img_batch):
        r, c = 3, 3

        imgs_gen_real, imgs_cond = img_batch
        imgs_gen = self.generator.predict([imgs_cond])

        imgs_all = [imgs_cond, imgs_gen_real, imgs_gen]

        # Rescale images 0 - 1
        for i in range(len(imgs_all)):
            imgs_all[i] = 0.5 * imgs_all[i] + 0.5

        titles = ['Condition', 'Original', 'Generated']

        fig, axs = plt.subplots(r, c)
        for i in range(r):
            for j in range(c):
                # RGB image:
                if titles[j] == 'Condition':
                    axs[i, j].imshow(imgs_all[j][i])
                # gray scale image:
                else:
                    if self.img_channels_target == 1:
                        axs[i, j].imshow(imgs_all[j][i, :, :, 0], cmap='gray')
                    else:
                        axs[i, j].imshow(imgs_all[j][i, ...])
                axs[i, j].set_title(titles[j])
                axs[i, j].axis('off')

        fig.savefig(GENERATED_DATA_LOCATION + self.name_string + '/' + '{}_{}.png'.format(epoch+1, repetition))
        plt.close()

    def save_generator(self, name):
        self.generator.save_weights(MODEL_WEIGHTS_PATH + 'generator_weights_' + str(name) + '.hdf5')

    def load_generator(self, name):
        self.generator.load_weights(MODEL_WEIGHTS_PATH + 'generator_weights_' + str(name) + '.hdf5')

    def apply_generator(self, tensor):
        # expect input to be of shape (num_samples, height, width, channels)
        tensor = np.array(tensor / 127.5 - 1, dtype=np.float32)
        img_out = self.generator.predict(tensor)
        return img_out


if __name__ == '__main__':
    esrgan = ESRGAN()
    esrgan.train_aerial()
