from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import pointfly as pf
import tensorflow as tf


def xconv(pts, fts, qrs, tag, N, K, D, P, C, C_pts_fts, is_training, with_X_transformation, depth_multiplier,
          sorting_method=None):
    if D == 1:
        _, indices = pf.knn_indices_general(qrs, pts, K, False)
    else:
        _, indices_dilated = pf.knn_indices_general(qrs, pts, K * D, True)
        indices = indices_dilated[:, :, ::D, :]

    if sorting_method is not None:
        indices = pf.sort_points(pts, indices, sorting_method)

    nn_pts = tf.gather_nd(pts, indices, name=tag + 'nn_pts')  # (N, P, K, 3)
    nn_pts_center = tf.expand_dims(qrs, axis=2, name=tag + 'nn_pts_center')  # (N, P, 1, 3)
    nn_pts_local = tf.subtract(nn_pts, nn_pts_center, name=tag + 'nn_pts_local')  # (N, P, K, 3)
    nn_pts_local_bn = pf.batch_normalization(nn_pts_local, is_training, name=tag + 'nn_pts_local_bn')

    # Prepare features to be transformed
    nn_fts_from_pts_0 = pf.dense(nn_pts_local_bn, C_pts_fts, tag + 'nn_fts_from_pts_0', is_training)
    nn_fts_from_pts = pf.dense(nn_fts_from_pts_0, C_pts_fts, tag + 'nn_fts_from_pt', is_training)
    if fts is None:
        nn_fts_input = nn_fts_from_pts
    else:
        nn_fts_from_prev = tf.gather_nd(fts, indices, name=tag + 'nn_fts_from_prev')
        nn_fts_input = tf.concat([nn_fts_from_pts, nn_fts_from_prev], axis=-1, name=tag + 'nn_fts_input')

    if with_X_transformation:
        ######################## X-transformation #########################
        X_0 = pf.conv2d(nn_pts_local_bn, K * K, tag + 'X_0', is_training, (1, K), with_bn=False)
        X_1 = pf.dense(X_0, K * K, tag + 'X_1', is_training, with_bn=False)
        X_2 = pf.dense(X_1, K * K, tag + 'X_2', is_training, with_bn=False, activation=None)
        X = tf.reshape(X_2, (N, P, K, K), name=tag + 'X')
        fts_X = tf.matmul(X, nn_fts_input, name=tag + 'fts_X')
        ###################################################################
    else:
        fts_X = nn_fts_input

    fts = pf.separable_conv2d(fts_X, C, tag + 'fts', is_training, (1, K), depth_multiplier=depth_multiplier)
    return tf.squeeze(fts, axis=2, name=tag + 'fts_3d')



def input_transform_net(point_cloud, is_training, N, PP, KK=3):
    """ Input (XYZ) Transform Net, input is BxNx3 gray image
        Return:
            Transformation matrix of size 3xK """

    input_image = tf.expand_dims(point_cloud, -1) # (N,PP,3,1)

    print(input_image)
    net = pf.conv2d(input_image, 64, 'tconv1', is_training,(1,3)) # (N,PP,1,64)
    net = pf.dense(net, 128, 'tconv2', is_training)      # (N,PP,1,128)
    net = pf.dense(net, 1024, 'tconv3', is_training)   # (N,PP,1,1024)
    net = pf.max_pool2d(net,[PP,1],'tmaxpool')    # (N,1,1,1024)
    net = pf.dense(net, 512, 'tfc1',is_training)
    net = pf.dense(net, 256, 'tfc2',is_training)    # (N,1,1,256)
    net = tf.reshape(net, (N, -1))
    with tf.variable_scope('transform_XYZ') as sc:
        assert(KK==3)
        weights = tf.get_variable('weights', (256, 3*KK),
                                  initializer=tf.constant_initializer(0.0),
                                  dtype=tf.float32)
        biases = tf.get_variable('biases', (3*KK),
                                 initializer=tf.constant_initializer(0.0),
                                 dtype=tf.float32)
        biases += tf.constant([1,0,0,0,1,0,0,0,1], dtype=tf.float32)
        transform = tf.matmul(net, weights)
        transform = tf.nn.bias_add(transform, biases)

    transform = tf.reshape(transform, (N, 3, KK))  #  (N,3,3)
    return transform

def pointnet1(point_cloud, is_training, N, PP):
    transform = input_transform_net(point_cloud, is_training, N, PP, KK=3)
    point_cloud_transformed = tf.matmul(point_cloud, transform)
    input_image = tf.expand_dims(point_cloud_transformed, -1) # (N,PP,3,1)
    net = pf.conv2d(input_image, 64, 'p1_conv1', is_training,(1,3)) # (N,PP,1,64)
    net = pf.dense(net, 64, 'p1_mlp1', is_training) # (N,PP,1,64)
    net = pf.dense(net, 64, 'p1_mlp2', is_training) # (N,PP,1,64)
    net = pf.dense(net, 128, 'p1_mlp3', is_training) # (N,PP,1,128)
    net = pf.dense(net, 1024, 'p1_mlp4', is_training) # (N,PP,1,1024)
    net1 = net
    net = pf.max_pool2d(net,[PP,1],'p1_maxpool')    # (N,1,1,1024)
    net = tf.squeeze(net, axis=[2])
    net1 = tf.squeeze(net1, axis=[2])
    return net,net1

class PointCNN:
    def __init__(self, points, features, num_class, is_training, setting, task):
        xconv_params = setting.xconv_params
        fc_params = setting.fc_params
        with_X_transformation = setting.with_X_transformation
        sorting_method = setting.sorting_method
        N = tf.shape(points)[0]

        fts_T,fts_T1 = pointnet1(points, is_training, N, PP=1024)

        if setting.with_fps:
            from sampling import tf_sampling

        self.layer_pts = [points]

        qrs_indices = pf.knn_indices_general(points, points, 1, False)
        self.layer_qrs_indices = [qrs_indices]

        if features is None:
            self.layer_fts = [features]
        else:
            C_fts = xconv_params[0][-1] // 2
            features_hd = pf.dense(features, C_fts, 'features_hd', is_training)
            self.layer_fts = [features_hd]

        for layer_idx, layer_param in enumerate(xconv_params):
            tag = 'xconv_' + str(layer_idx + 1) + '_'
            K, D, P, C = layer_param

            # get k-nearest points
            pts = self.layer_pts[-1]
            fts = self.layer_fts[-1]
            if P == -1:
                qrs = points
                _, qrs_indices = pf.knn_indices_general(qrs, points, 1, False)   #### zj 2018/04/10
            else:
                if setting.with_fps:
                    qrs = tf_sampling.gather_point(pts, qrs_indices)  # (N,P,3)
                else:
                    qrs = tf.slice(pts, (0, 0, 0), (-1, P, -1), name=tag + 'qrs')  # (N, P, 3)
                _, qrs_indices = pf.knn_indices_general(qrs, points, 1, False)     #### zj 2018/04/10

            print(qrs_indices)                                    #### zj 2018/04/10
            self.layer_qrs_indices.append(qrs_indices)
            self.layer_pts.append(qrs)
 
            if layer_idx == 0:
                C_pts_fts = C // 2 if fts is None else C // 4
                depth_multiplier = 4
            else:
                C_prev = xconv_params[layer_idx - 1][-1]
                C_pts_fts = C_prev // 4
                depth_multiplier = math.ceil(C / C_prev)
            fts_xconv = xconv(pts, fts, qrs, tag, N, K, D, P, C, C_pts_fts, is_training, with_X_transformation,
                              depth_multiplier, sorting_method)
            self.layer_fts.append(fts_xconv)

        if task == 'segmentation':
            for layer_idx, layer_param in enumerate(setting.xdconv_params):
                tag = 'xdconv_' + str(layer_idx + 1) + '_'
                K, D, pts_layer_idx, qrs_layer_idx = layer_param

                pts = self.layer_pts[pts_layer_idx + 1]
                fts = self.layer_fts[pts_layer_idx + 1] if layer_idx == 0 else self.layer_fts[-1]
                qrs = self.layer_pts[qrs_layer_idx + 1]
                fts_qrs = self.layer_fts[qrs_layer_idx + 1]
                _, _, P, C = xconv_params[qrs_layer_idx]
                _, _, _, C_prev = xconv_params[pts_layer_idx]
                C_pts_fts = C_prev // 4
                depth_multiplier = 1
                fts_xdconv = xconv(pts, fts, qrs, tag, N, K, D, P, C, C_pts_fts, is_training, with_X_transformation,
                                   depth_multiplier, sorting_method)
                fts_concat = tf.concat([fts_xdconv, fts_qrs], axis=-1, name=tag + 'fts_concat')
                fts_fuse = pf.dense(fts_concat, C, tag + 'fts_fuse', is_training)
                self.layer_pts.append(qrs)
                self.layer_fts.append(fts_fuse)
        #################################### zj,2018/04/10####################################
        self.fc_layers1 = self.layer_fts[-1]
        self.fc_layers_indices = self.layer_qrs_indices[-1]
        #last_ftsT = tf.gather_nd(fts_T, self.fc_layers_indices , name='last_ftsT')
        #last_ftsT = tf.squeeze(last_ftsT, axis=2, name='last_ftsT1')
        nn = tf.shape(self.fc_layers1)[1]
        last_ftsT = tf.tile(fts_T, (1,nn,1),name = 'last_ftsT')
        self.fc_layers1 = tf.concat([self.fc_layers1, last_ftsT] ,axis = -1, name='last_ftsT_concat')
        self.fc_layers = [self.fc_layers1]
        print(self.fc_layers)
        #######################################################################################

        for layer_idx, layer_param in enumerate(fc_params):
            channel_num, drop_rate = layer_param
            fc = pf.dense(self.fc_layers[-1], channel_num, 'fc{:d}'.format(layer_idx), is_training)  #[N,128,channel_num]???
            fc_drop = tf.layers.dropout(fc, drop_rate, training=is_training, name='fc{:d}_drop'.format(layer_idx))
            self.fc_layers.append(fc_drop)

        logits = pf.dense(self.fc_layers[-1], num_class, 'logits', is_training, with_bn=False, activation=None) #[N,128,40?]???
        if task == 'classification':
            logits_mean = tf.reduce_mean(logits, axis=1, keep_dims=True, name='logits_mean')
            self.logits = tf.cond(is_training, lambda: logits, lambda: logits_mean)
        elif task == 'segmentation':
            self.logits = logits
        else:
            print('Unknown task!')
            exit()
        self.probs = tf.nn.softmax(self.logits, name='probs')
