"""
    SQNet for image segmentation, implemented in PyTorch.
    Original paper: 'Speeding up Semantic Segmentation for Autonomous Driving,'
    https://https://openreview.net/pdf?id=S1uHiFyyg.
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from common import conv1x1, conv1x1_block, conv3x3_block, deconv3x3_block, InterpolationBlock, Hourglass, Identity,\
    Concurrent


class FireBlock(nn.Module):
    def __init__(self,
                 in_channels,
                 out_channels,
                 bias=True,
                 use_bn=False,
                 activation=(lambda: nn.ELU(inplace=True))):
        super(FireBlock, self).__init__()
        squeeze_channels = out_channels // 8
        expand_channels = out_channels // 2

        self.conv = conv1x1_block(
            in_channels=in_channels,
            out_channels=squeeze_channels,
            bias=bias,
            use_bn=use_bn,
            activation=activation)
        self.branches = Concurrent(merge_type="cat")
        self.branches.add_module("branch1", conv1x1_block(
            in_channels=squeeze_channels,
            out_channels=expand_channels,
            bias=bias,
            use_bn=use_bn,
            activation=None))
        self.branches.add_module("branch2", conv3x3_block(
            in_channels=squeeze_channels,
            out_channels=expand_channels,
            bias=bias,
            use_bn=use_bn,
            activation=None))
        self.activ = nn.ELU(inplace=True)

    def forward(self, x):
        x = self.conv(x)
        x = self.branches(x)
        x = self.activ(x)
        return x


class ParallelDilatedConv(nn.Module):
    def __init__(self,
                 in_channels,
                 out_channels,
                 bias=True,
                 use_bn=False,
                 activation=(lambda: nn.ELU(inplace=True))):
        super(ParallelDilatedConv, self).__init__()
        dilations = [1, 2, 3, 4]

        self.branches = Concurrent(merge_type="sum")
        for i, dilation in enumerate(dilations):
            self.branches.add_module("branch{}".format(i + 1), conv3x3_block(
                in_channels=in_channels,
                out_channels=out_channels,
                padding=dilation,
                dilation=dilation,
                bias=bias,
                use_bn=use_bn,
                activation=activation))

    def forward(self, x):
        x = self.branches(x)
        return x


class SQNetUpStage(nn.Module):
    def __init__(self,
                 in_channels,
                 out_channels,
                 bias=True,
                 use_bn=False,
                 activation=(lambda: nn.ELU(inplace=True)),
                 use_parallel=False):
        super(SQNetUpStage, self).__init__()

        if use_parallel:
            self.conv = ParallelDilatedConv(
                in_channels=in_channels,
                out_channels=in_channels,
                bias=bias,
                use_bn=use_bn,
                activation=activation)
        else:
            self.conv = conv3x3_block(
                in_channels=in_channels,
                out_channels=in_channels,
                bias=bias,
                use_bn=use_bn,
                activation=activation)
        self.deconv = deconv3x3_block(
            in_channels=in_channels,
            out_channels=out_channels,
            stride=2,
            bias=bias,
            use_bn=use_bn,
            activation=activation)

    def forward(self, x):
        x = self.conv(x)
        x = self.deconv(x)
        return x


class SQNet(nn.Module):
    def __init__(self,
                 aux=False,
                 fixed_size=False,
                 in_channels=3,
                 in_size=(1024, 2048),
                 num_classes=19):
        super().__init__()
        bias = True
        use_bn = False
        activation = (lambda: nn.ELU(inplace=True))

        init_block_channels = 96

        self.stem = conv3x3_block(
            in_channels=in_channels,
            out_channels=init_block_channels,
            stride=2,
            bias=bias,
            use_bn=use_bn,
            activation=activation)
        in_channels = init_block_channels

        channels = [[128, 256, 512], [256, 128, 96]]
        layers = [2, 2, 3]

        down_seq = nn.Sequential()
        skip_seq = nn.Sequential()
        for i, out_channels in enumerate(channels[0]):
            skip_seq.add_module("skip{}".format(i + 1), conv3x3_block(
                in_channels=in_channels,
                out_channels=in_channels,
                bias=bias,
                use_bn=use_bn,
                activation=activation))
            stage = nn.Sequential()
            stage.add_module("unit1", nn.MaxPool2d(
                kernel_size=2,
                stride=2))
            for j in range(layers[i]):
                stage.add_module("unit{}".format(j + 2), FireBlock(
                    in_channels=in_channels,
                    out_channels=out_channels,
                    bias=bias,
                    use_bn=use_bn,
                    activation=activation))
                in_channels = out_channels
            down_seq.add_module("down{}".format(i + 1), stage)

        in_channels = in_channels // 2

        up_seq = nn.Sequential()
        for i, out_channels in enumerate(channels[1]):
            use_parallel = True if i == 0 else False
            up_seq.add_module("up{}".format(i + 1), SQNetUpStage(
                in_channels=(2 * in_channels),
                out_channels=out_channels,
                bias=bias,
                use_bn=use_bn,
                activation=activation,
                use_parallel=use_parallel))
            in_channels = out_channels
        up_seq = up_seq[::-1]

        self.hg = Hourglass(
            down_seq=down_seq,
            up_seq=up_seq,
            skip_seq=skip_seq,
            merge_type="cat")

        self.head = SQNetUpStage(
            in_channels=(2 * in_channels),
            out_channels=num_classes,
            bias=bias,
            use_bn=use_bn,
            activation=activation,
            use_parallel=False)

    def forward(self, x):
        x = self.stem(x)
        x = self.hg(x)
        x = self.head(x)
        return x



def oth_sqnet_cityscapes(num_classes=19, pretrained=False, **kwargs):
    return SQNet(num_classes=num_classes, **kwargs)


def _calc_width(net):
    import numpy as np
    net_params = filter(lambda p: p.requires_grad, net.parameters())
    weight_count = 0
    for param in net_params:
        weight_count += np.prod(param.size())
    return weight_count


def _test():
    pretrained = False
    # fixed_size = True
    in_size = (1024, 2048)
    classes = 19

    models = [
        oth_sqnet_cityscapes,
    ]

    for model in models:

        # from torchsummary import summary
        # device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        # net = SQNet(num_classes=19).to(device)
        # summary(net, (3, 512, 1024))

        net = model(pretrained=pretrained)

        # net.train()
        net.eval()
        weight_count = _calc_width(net)
        print("m={}, {}".format(model.__name__, weight_count))
        assert (model != oth_sqnet_cityscapes or weight_count == 16262771)

        batch = 4
        x = torch.randn(batch, 3, in_size[0], in_size[1])
        y = net(x)
        # y.sum().backward()
        assert (tuple(y.size()) == (batch, classes, in_size[0], in_size[1]))


if __name__ == "__main__":
    _test()
