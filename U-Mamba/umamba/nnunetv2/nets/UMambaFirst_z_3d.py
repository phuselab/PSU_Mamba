import numpy as np
import math
import torch
from torch import nn
from torch.nn import functional as F
from typing import Union, Type, List, Tuple

from dynamic_network_architectures.building_blocks.helper import get_matching_convtransp

from torch.nn.modules.conv import _ConvNd
from torch.nn.modules.dropout import _DropoutNd
from dynamic_network_architectures.building_blocks.helper import convert_conv_op_to_dim

from nnunetv2.utilities.plans_handling.plans_handler import ConfigurationManager, PlansManager
from dynamic_network_architectures.building_blocks.helper import get_matching_instancenorm, convert_dim_to_conv_op
from dynamic_network_architectures.initialization.weight_init import init_last_bn_before_add_to_0
from nnunetv2.utilities.network_initialization import InitWeights_He
from mamba_ssm import Mamba
from dynamic_network_architectures.building_blocks.helper import maybe_convert_scalar_to_list, get_matching_pool_op
from torch.cuda.amp import autocast
from dynamic_network_architectures.building_blocks.residual import BasicBlockD
import logging

import torch.nn as nn
from torch.cuda.amp import autocast
from mamba_ssm.modules.mamba_simple import Mamba

from sklearn.decomposition import PCA

logging.basicConfig(
    filename='umamba_scan_path.log',
    filemode='a',  # append mode
    format='%(asctime)s %(levelname)s:%(message)s',
    level=logging.INFO
)

def flatten_for_scan(x, scan_type='z'):
    """
    Input: x: (B, C, D, H, W) tensor
    Output: Flattened tensor and unpermute function
    scan_type: 'x', 'y', 'z', 'yz-diag', 'xy-diag'
    - 'x': Flatten along the x-axis
    - 'y': Flatten along the y-axis
    - 'z': Flatten along the z-axis
    - 'yz-diag': Flatten along the diagonal of the yz-plane
    - 'xy-diag': Flatten along the diagonal of the xy-plane
    """
    B, C, D, H, W = x.shape

    if scan_type == 'x':
        print("Using x-scan")
        x_perm = x.permute(0, 1, 4, 3, 2)  # (B, C, X, Y, Z)
        flatten = x_perm.reshape(B, C, -1).transpose(-1, -2)
        unpermute = lambda t: t.reshape(B, C, W, H, D).permute(0, 1, 4, 3, 2)
        return flatten, unpermute

    elif scan_type == 'y':
        print("Using y-scan")
        x_perm = x.permute(0, 1, 3, 4, 2)  # (B, C, Y, X, Z)
        flatten = x_perm.reshape(B, C, -1).transpose(-1, -2)
        unpermute = lambda t: t.reshape(B, C, H, W, D).permute(0, 1, 4, 2, 3)
        return flatten, unpermute

    elif scan_type == 'z':
        #print("Using z-scan")
        # No permutation
        flatten = x.reshape(B, C, -1).transpose(-1, -2)
        unpermute = lambda t: t.transpose(-1, -2).reshape(B, C, D, H, W)
        return flatten, unpermute

    elif scan_type == 'yz-diag':
        print("Using yz-diag-scan")
        x_perm = x.permute(0, 1, 4, 3, 2)  # (B, C, X, Y, Z)
        X, Y, Z = x_perm.shape[2:]
        z_coords, y_coords = torch.meshgrid(
            torch.arange(Z, device=x.device),
            torch.arange(Y, device=x.device),
            indexing='ij'
        )
        
        diag_order = torch.argsort((z_coords + y_coords).flatten()) #! changed
        x_flat = x_perm.reshape(B, C, X, Y * Z)[:, :, :, diag_order]
        flatten = x_flat.reshape(B, C, -1).transpose(-1, -2)
        unpermute = lambda t: t.reshape(B, C, X, Y, Z).permute(0, 1, 4, 3, 2)
        return flatten, unpermute

    elif scan_type == 'xy-diag':
        x_perm = x.permute(0, 1, 2, 4, 3)  # (B, C, Z, X, Y)
        D, X, Y = x_perm.shape[2:]
        x_coords, y_coords = torch.meshgrid(
            torch.arange(X, device=x.device),
            torch.arange(Y, device=x.device),
            indexing='ij'
        )
        
        diag_order = torch.argsort((x_coords + y_coords).flatten()) #! changed
        x_flat = x_perm.reshape(B, C, D, X * Y)[:, :, :, diag_order]
        flatten = x_flat.reshape(B, C, -1).transpose(-1, -2)
        unpermute = lambda t: t.reshape(B, C, D, X, Y).permute(0, 1, 2, 4, 3)
        return flatten, unpermute

    else:
        raise ValueError(f"Unsupported scan type: {scan_type}")

    


def flatten_pca_scan(x, principal_vector):
    """
    Flattens the input tensor x using the given principal_vector.
    Input:
        x: (B, C, D, H, W) tensor
        principal_vector: (3,) numpy or torch array (should be unit vector)
    Output:
        x_flat: (B, N, C) tensor, where N = D*H*W
        indices: list of sorted indices for each batch
    """
    B, C, D, H, W = x.shape
    device = x.device
    # Create a grid of coordinates (D, H, W, 3)
    zz, yy, xx = torch.meshgrid(
        torch.arange(D, device=device),
        torch.arange(H, device=device),
        torch.arange(W, device=device),
        indexing='ij'
    )
    coords = torch.stack([zz, yy, xx], dim=-1).reshape(-1, 3).float()  # (N, 3)
    # Project coordinates onto principal vector
    if isinstance(principal_vector, np.ndarray):
        principal_vector = torch.from_numpy(principal_vector).to(device, dtype=torch.float32)
    else:
        principal_vector = principal_vector.to(device, dtype=torch.float32)
    projections = torch.matmul(coords, principal_vector)  # (N,)
    sorted_indices = torch.argsort(projections)
    # Flatten x and reorder according to sorted_indices
    x_flat_list = []
    index_list = []
    for b in range(B):
        x_b = x[b].reshape(C, -1)[:, sorted_indices].T  # (N, C)
        x_flat_list.append(x_b)
        index_list.append(sorted_indices)
    x_flat = torch.stack(x_flat_list, dim=0)  # (B, N, C)
    # unpermute function to restore original shape
    def unpermute(t):
        # t: (B, N, C)
        out = torch.zeros((B, D * H * W, C), device=t.device, dtype=t.dtype)
        for b in range(B):
            out[b, sorted_indices] = t[b]
        return out.permute(0, 2, 1).reshape(B, C, D, H, W)
    return x_flat, unpermute



class UpsampleLayer(nn.Module):
    def __init__(
            self,
            conv_op,
            input_channels,
            output_channels,
            pool_op_kernel_size,
            mode='nearest'
        ):
        super().__init__()
        self.conv = conv_op(input_channels, output_channels, kernel_size=1)
        self.pool_op_kernel_size = pool_op_kernel_size
        self.mode = mode
        
    def forward(self, x):
        x = F.interpolate(x, scale_factor=self.pool_op_kernel_size, mode=self.mode)
        x = self.conv(x)
        return x

class MambaLayer(nn.Module):
    #  
    # - check patch size to avoid segmenting ears
    # - scan paths
    # - put mamba also in encoding layers
    def __init__(self, dim, d_state = 16, d_conv = 4, expand = 2, scan_type='z'): #  
            
        super().__init__()
        self.dim = dim
        self.norm = nn.LayerNorm(dim)
        self.mamba = Mamba(
                d_model=dim, # Model dimension d_model
                d_state=d_state,  # SSM state expansion factor
                d_conv=d_conv,    # Local convolution width
                expand=expand,    # Block expansion factor
        )
        self.scan_type = scan_type # self.scan_type = 'x'  # 'x', 'y', 'z', 'xy-diag', 'yz-diag', 'xz-diag'
        self.register_buffer('principal_vector', torch.zeros(3), persistent=True)

        
    ## NEW VERSION FROM HERE: 
    
    def set_scan_vector(self, vector):
        # Accepts numpy or torch, moves to correct device and dtype
        if isinstance(vector, np.ndarray):
            vector = torch.from_numpy(vector)
        self.principal_vector.copy_(vector.to(self.principal_vector.device, dtype=self.principal_vector.dtype))

    @autocast(enabled=False)
    def forward(self, x):
        if x.dtype == torch.float16:
            x = x.type(torch.float32)

        B, C = x.shape[:2]
        assert C == self.dim
        n_tokens = x.shape[2:].numel() # total number of voxels in the 3D image (i.e., Z × Y × X)
        img_dims = x.shape[2:]
        

        if self.scan_type == 'pca':
            if not torch.any(self.principal_vector):
            # Fallback to x-scan if PCA vector is not yet set
                print("[MambaLayer] PCA vector not set. Falling back to 'x' scan.")
                logging.info("[MambaLayer] PCA vector not set. Falling back to 'x' scan.")
                x_flat, unpermute = flatten_for_scan(x, 'x')
                x_norm = self.norm(x_flat)
                x_mamba = self.mamba(x_norm)
                out = x_mamba.transpose(-1, -2).reshape(B, C, *img_dims)
                return unpermute(out)
            #print("[MambaLayer] Using PCA scan with principal vector:", self.principal_vector)
            x_flat, unpermute = flatten_pca_scan(x, self.principal_vector)
            x_norm = self.norm(x_flat)
            x_mamba = self.mamba(x_norm)

            # x_mamba is (B, N, C), unpermute expects (B, N, C)
            return unpermute(x_mamba)

        else:
            logging.info(f"[MambaLayer] Using scan type: {self.scan_type}")
            x_flat, unpermute = flatten_for_scan(x, self.scan_type)
            x_norm = self.norm(x_flat)
            x_mamba = self.mamba(x_norm)
            out = x_mamba.transpose(-1, -2).reshape(B, C, *img_dims)
            return unpermute(out)

    
    ## NEW VERSION UNTIL # delete what comes after
    

class BasicResBlock(nn.Module):
    def __init__(
            self,
            conv_op,
            input_channels,
            output_channels,
            norm_op,
            norm_op_kwargs,
            kernel_size=3,
            padding=1,
            stride=1,
            use_1x1conv=False,
            nonlin=nn.LeakyReLU,
            nonlin_kwargs={'inplace': True}
        ):
        super().__init__()

        
        self.conv1 = conv_op(input_channels, output_channels, kernel_size, stride=stride, padding=padding)
        self.norm1 = norm_op(output_channels, **norm_op_kwargs)
        self.act1 = nonlin(**nonlin_kwargs)
        
        self.conv2 = conv_op(output_channels, output_channels, kernel_size, padding=padding)
        self.norm2 = norm_op(output_channels, **norm_op_kwargs)
        self.act2 = nonlin(**nonlin_kwargs)
        
        if use_1x1conv:
            self.conv3 = conv_op(input_channels, output_channels, kernel_size=1, stride=stride)
        else:
            self.conv3 = None
                  
    def forward(self, x):
        y = self.conv1(x)
        y = self.act1(self.norm1(y))  
        y = self.norm2(self.conv2(y))
        if self.conv3:
            x = self.conv3(x)
        y += x
        return self.act2(y)
    
class UNetResEncoder(nn.Module):
    def __init__(self,
                 input_channels: int,
                 n_stages: int,
                 features_per_stage: Union[int, List[int], Tuple[int, ...]],
                 conv_op: Type[_ConvNd],
                 kernel_sizes: Union[int, List[int], Tuple[int, ...]],
                 strides: Union[int, List[int], Tuple[int, ...], Tuple[Tuple[int, ...], ...]],
                 n_blocks_per_stage: Union[int, List[int], Tuple[int, ...]],
                 conv_bias: bool = False,
                 norm_op: Union[None, Type[nn.Module]] = None,
                 norm_op_kwargs: dict = None,
                 nonlin: Union[None, Type[torch.nn.Module]] = None,
                 nonlin_kwargs: dict = None,
                 return_skips: bool = False,
                 stem_channels: int = None,
                 pool_type: str = 'conv',
                 ):
        super().__init__()
        if isinstance(kernel_sizes, int):
            kernel_sizes = [kernel_sizes] * n_stages
        if isinstance(features_per_stage, int):
            features_per_stage = [features_per_stage] * n_stages
        if isinstance(n_blocks_per_stage, int):
            n_blocks_per_stage = [n_blocks_per_stage] * n_stages
        if isinstance(strides, int):
            strides = [strides] * n_stages

        assert len(
            kernel_sizes) == n_stages, "kernel_sizes must have as many entries as we have resolution stages (n_stages)"
        assert len(
            n_blocks_per_stage) == n_stages, "n_conv_per_stage must have as many entries as we have resolution stages (n_stages)"
        assert len(
            features_per_stage) == n_stages, "features_per_stage must have as many entries as we have resolution stages (n_stages)"
        assert len(strides) == n_stages, "strides must have as many entries as we have resolution stages (n_stages). " \
                                         "Important: first entry is recommended to be 1, else we run strided conv drectly on the input"

        pool_op = get_matching_pool_op(conv_op, pool_type=pool_type) if pool_type != 'conv' else None

        self.conv_pad_sizes = []
        for krnl in kernel_sizes:
            self.conv_pad_sizes.append([i // 2 for i in krnl])

        stem_channels = features_per_stage[0]

        # The above code snippet is defining a stem module in a neural network using PyTorch. It
        # consists of a sequence of layers, specifically a BasicResBlock, which is a basic residual
        # block. The parameters being passed to the BasicResBlock include the type of convolution
        # operation (conv_op), input and output channels, normalization operation (norm_op),
        # normalization operation arguments (norm_op_kwargs), kernel size, padding size, stride,
        # nonlinearity function (nonlin), nonlinearity function arguments (nonlin_kwargs), and a flag
        # indicating whether to use a 1x1 convolution layer (use_
        self.stem = nn.Sequential(
            BasicResBlock(
                conv_op = conv_op,
                input_channels = input_channels,
                output_channels = stem_channels,
                norm_op=norm_op,
                norm_op_kwargs=norm_op_kwargs,
                kernel_size=kernel_sizes[0],
                padding=self.conv_pad_sizes[0],
                stride=1,
                nonlin=nonlin,
                nonlin_kwargs=nonlin_kwargs,
                use_1x1conv=True
            ), 
            *[
                BasicBlockD(
                    conv_op = conv_op,
                    input_channels = stem_channels,
                    output_channels = stem_channels,
                    kernel_size = kernel_sizes[0],
                    stride = 1,
                    conv_bias = conv_bias,
                    norm_op = norm_op,
                    norm_op_kwargs = norm_op_kwargs,
                    nonlin = nonlin,
                    nonlin_kwargs = nonlin_kwargs,
                ) for _ in range(n_blocks_per_stage[0] - 1)
            ]
        )


        input_channels = stem_channels

        stages = []
        for s in range(n_stages):

            stage = nn.Sequential(
                BasicResBlock(
                    conv_op = conv_op,
                    norm_op = norm_op,
                    norm_op_kwargs = norm_op_kwargs,
                    input_channels = input_channels,
                    output_channels = features_per_stage[s],
                    kernel_size = kernel_sizes[s],
                    padding=self.conv_pad_sizes[s],
                    stride=strides[s],
                    use_1x1conv=True,
                    nonlin = nonlin,
                    nonlin_kwargs = nonlin_kwargs
                ),
                *[
                    BasicBlockD(
                        conv_op = conv_op,
                        input_channels = features_per_stage[s],
                        output_channels = features_per_stage[s],
                        kernel_size = kernel_sizes[s],
                        stride = 1,
                        conv_bias = conv_bias,
                        norm_op = norm_op,
                        norm_op_kwargs = norm_op_kwargs,
                        nonlin = nonlin,
                        nonlin_kwargs = nonlin_kwargs,
                    ) for _ in range(n_blocks_per_stage[s] - 1)
                ]
            )


            stages.append(stage)
            input_channels = features_per_stage[s]

        self.stages = nn.Sequential(*stages)
        self.output_channels = features_per_stage
        self.strides = [maybe_convert_scalar_to_list(conv_op, i) for i in strides]
        self.return_skips = return_skips

        self.conv_op = conv_op
        self.norm_op = norm_op
        self.norm_op_kwargs = norm_op_kwargs
        self.nonlin = nonlin
        self.nonlin_kwargs = nonlin_kwargs

        self.conv_bias = conv_bias
        self.kernel_sizes = kernel_sizes

    def forward(self, x):
        #print("UNetResEncoder x.shape:", x.shape)
        if self.stem is not None:
            x = self.stem(x)
        ret = []
        for s in self.stages:
            #print("UNetResEncoder s(x).shape for stage s", s , s(x).shape)
            x = s(x)
            ret.append(x)
        if self.return_skips:
            return ret
        else:
            return ret[-1]

    def compute_conv_feature_map_size(self, input_size):
        if self.stem is not None:
            output = self.stem.compute_conv_feature_map_size(input_size)
        else:
            output = np.int64(0)

        for s in range(len(self.stages)):
            output += self.stages[s].compute_conv_feature_map_size(input_size)
            input_size = [i // j for i, j in zip(input_size, self.strides[s])]

        return output


class UNetResDecoder(nn.Module):
    def __init__(self,
                 encoder,
                 num_classes,
                 n_conv_per_stage: Union[int, Tuple[int, ...], List[int]],
                 deep_supervision, nonlin_first: bool = False):

        super().__init__()
        self.deep_supervision = deep_supervision
        self.encoder = encoder
        self.num_classes = num_classes
        n_stages_encoder = len(encoder.output_channels)
        if isinstance(n_conv_per_stage, int):
            n_conv_per_stage = [n_conv_per_stage] * (n_stages_encoder - 1)
        assert len(n_conv_per_stage) == n_stages_encoder - 1, "n_conv_per_stage must have as many entries as we have " \
                                                          "resolution stages - 1 (n_stages in encoder - 1), " \
                                                          "here: %d" % n_stages_encoder


        stages = []
        upsample_layers = []

        seg_layers = []
        for s in range(1, n_stages_encoder):
            input_features_below = encoder.output_channels[-s]
            input_features_skip = encoder.output_channels[-(s + 1)]
            stride_for_upsampling = encoder.strides[-s]


            upsample_layers.append(UpsampleLayer(
                conv_op = encoder.conv_op,
                input_channels = input_features_below,
                output_channels = input_features_skip,
                pool_op_kernel_size = stride_for_upsampling,
                mode='nearest'
            ))

            stages.append(nn.Sequential(
                BasicResBlock(
                    conv_op = encoder.conv_op,
                    norm_op = encoder.norm_op,
                    norm_op_kwargs = encoder.norm_op_kwargs,
                    nonlin = encoder.nonlin,
                    nonlin_kwargs = encoder.nonlin_kwargs,
                    input_channels = 2 * input_features_skip,
                    output_channels = input_features_skip,
                    kernel_size = encoder.kernel_sizes[-(s + 1)],
                    padding=encoder.conv_pad_sizes[-(s + 1)],
                    stride=1,
                    use_1x1conv=True
                ),
                *[
                    BasicBlockD(
                        conv_op = encoder.conv_op,
                        input_channels = input_features_skip,
                        output_channels = input_features_skip,
                        kernel_size = encoder.kernel_sizes[-(s + 1)],
                        stride = 1,
                        conv_bias = encoder.conv_bias,
                        norm_op = encoder.norm_op,
                        norm_op_kwargs = encoder.norm_op_kwargs,
                        nonlin = encoder.nonlin,
                        nonlin_kwargs = encoder.nonlin_kwargs,
                    ) for _ in range(n_conv_per_stage[s-1] - 1)

                ]
            ))
            seg_layers.append(encoder.conv_op(input_features_skip, num_classes, 1, 1, 0, bias=True))

        self.stages = nn.ModuleList(stages)
        self.upsample_layers = nn.ModuleList(upsample_layers)
        self.seg_layers = nn.ModuleList(seg_layers)

    def forward(self, skips):
        lres_input = skips[-1]
        seg_outputs = []
        for s in range(len(self.stages)):
            x = self.upsample_layers[s](lres_input)
            x = torch.cat((x, skips[-(s+2)]), 1)
            x = self.stages[s](x)
            if self.deep_supervision:
                seg_outputs.append(self.seg_layers[s](x))
            elif s == (len(self.stages) - 1):
                seg_outputs.append(self.seg_layers[-1](x))
            lres_input = x

        seg_outputs = seg_outputs[::-1]

        if not self.deep_supervision:
            r = seg_outputs[0]
        else:
            r = seg_outputs
        return r

    def compute_conv_feature_map_size(self, input_size):
        skip_sizes = []
        for s in range(len(self.encoder.strides) - 1):
            skip_sizes.append([i // j for i, j in zip(input_size, self.encoder.strides[s])])
            input_size = skip_sizes[-1]

        assert len(skip_sizes) == len(self.stages)

        output = np.int64(0)
        for s in range(len(self.stages)):
            output += self.stages[s].compute_conv_feature_map_size(skip_sizes[-(s+1)])
            output += np.prod([self.encoder.output_channels[-(s+2)], *skip_sizes[-(s+1)]], dtype=np.int64)
            if self.deep_supervision or (s == (len(self.stages) - 1)):
                output += np.prod([self.num_classes, *skip_sizes[-(s+1)]], dtype=np.int64)
        return output
    
class UMambaFirst(nn.Module):
    def __init__(self,
                 input_channels: int,
                 n_stages: int,
                 features_per_stage: Union[int, List[int], Tuple[int, ...]],
                 conv_op: Type[_ConvNd],
                 kernel_sizes: Union[int, List[int], Tuple[int, ...]],
                 strides: Union[int, List[int], Tuple[int, ...]],
                 n_conv_per_stage: Union[int, List[int], Tuple[int, ...]],
                 num_classes: int,
                 n_conv_per_stage_decoder: Union[int, Tuple[int, ...], List[int]],
                 conv_bias: bool = False,
                 norm_op: Union[None, Type[nn.Module]] = None,
                 norm_op_kwargs: dict = None,
                 dropout_op: Union[None, Type[_DropoutNd]] = None,
                 dropout_op_kwargs: dict = None,
                 nonlin: Union[None, Type[torch.nn.Module]] = None,
                 nonlin_kwargs: dict = None,
                 deep_supervision: bool = False,
                 stem_channels: int = None,
                 mamba_scan_type: str = 'z', #
                 ):
        super().__init__()
        n_blocks_per_stage = n_conv_per_stage
        if isinstance(n_blocks_per_stage, int):
            n_blocks_per_stage = [n_blocks_per_stage] * n_stages
        if isinstance(n_conv_per_stage_decoder, int):
            n_conv_per_stage_decoder = [n_conv_per_stage_decoder] * (n_stages - 1)
        

        for s in range(math.ceil(n_stages / 2), n_stages):
            n_blocks_per_stage[s] = 1    

        for s in range(math.ceil((n_stages - 1) / 2 + 0.5), n_stages - 1):
            n_conv_per_stage_decoder[s] = 1

        assert len(n_blocks_per_stage) == n_stages, "n_blocks_per_stage must have as many entries as we have " \
                                                  f"resolution stages. here: {n_stages}. " \
                                                  f"n_blocks_per_stage: {n_blocks_per_stage}"
        assert len(n_conv_per_stage_decoder) == (n_stages - 1), "n_conv_per_stage_decoder must have one less entries " \
                                                                f"as we have resolution stages. here: {n_stages} " \
                                                                f"stages, so it should have {n_stages - 1} entries. " \
                                                                f"n_conv_per_stage_decoder: {n_conv_per_stage_decoder}"
        self.encoder = UNetResEncoder(
            input_channels,
            n_stages,
            features_per_stage,
            conv_op,
            kernel_sizes,
            strides,
            n_blocks_per_stage,
            conv_bias,
            norm_op,
            norm_op_kwargs,
            nonlin,
            nonlin_kwargs,
            return_skips=True,
            stem_channels=stem_channels
        )
        self.mamba_scan_type = mamba_scan_type
        self.mamba_layer = MambaLayer(dim = features_per_stage[0], scan_type = mamba_scan_type) #! changed to have it in first layer directly

        self.decoder = UNetResDecoder(self.encoder, num_classes, n_conv_per_stage_decoder, deep_supervision)

    def forward(self, x):
        #print("IN U-Mamba First now")
        # ([2, 1, 160, 128, 112]))
        skips = self.encoder(x)
        skips[0] = self.mamba_layer(skips[0]) #! changed to incorporate it in first layer 
        return self.decoder(skips)

    def compute_conv_feature_map_size(self, input_size):
        assert len(input_size) == convert_conv_op_to_dim(self.encoder.conv_op), "just give the image size without color/feature channels or " \
                                                                                "batch channel. Do not give input_size=(b, c, x, y(, z)). " \
                                                                                "Give input_size=(x, y(, z))!"
        return self.encoder.compute_conv_feature_map_size(input_size) + self.decoder.compute_conv_feature_map_size(input_size)


def get_umamba_first_3d_from_plans(
        plans_manager: PlansManager,
        dataset_json: dict,
        configuration_manager: ConfigurationManager,
        num_input_channels: int,
        deep_supervision: bool = True,
        mamba_scan_type: str = 'z'  # 'x', 'y', 'z', 'xy-diag', 'yz-diag', 'xz-diag', 'pca' # 
    ):
    num_stages = len(configuration_manager.conv_kernel_sizes)

    dim = len(configuration_manager.conv_kernel_sizes[0])
    conv_op = convert_dim_to_conv_op(dim)

    label_manager = plans_manager.get_label_manager(dataset_json)

    segmentation_network_class_name = 'UMambaFirst'
    network_class = UMambaFirst
    kwargs = {
        'UMambaFirst': {
            'conv_bias': True,
            'norm_op': get_matching_instancenorm(conv_op),
            'norm_op_kwargs': {'eps': 1e-5, 'affine': True},
            'dropout_op': None, 'dropout_op_kwargs': None,
            'nonlin': nn.LeakyReLU, 'nonlin_kwargs': {'inplace': True},
            'mamba_scan_type': mamba_scan_type,  # 'x', 'y', 'z', 'xy-diag', 'yz-diag', 'xz-diag', 'pca'
        }
    }

    conv_or_blocks_per_stage = {
        'n_conv_per_stage': configuration_manager.n_conv_per_stage_encoder,
        'n_conv_per_stage_decoder': configuration_manager.n_conv_per_stage_decoder
    }

    model = network_class(
        input_channels=num_input_channels,
        n_stages=num_stages,
        features_per_stage=[min(configuration_manager.UNet_base_num_features * 2 ** i,
                                configuration_manager.unet_max_num_features) for i in range(num_stages)],
        conv_op=conv_op,
        kernel_sizes=configuration_manager.conv_kernel_sizes,
        strides=configuration_manager.pool_op_kernel_sizes,
        num_classes=label_manager.num_segmentation_heads,
        deep_supervision=deep_supervision,
        **conv_or_blocks_per_stage,
        **kwargs[segmentation_network_class_name]
    )
    model.apply(InitWeights_He(1e-2))

    return model
