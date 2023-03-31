import torch
import torch.nn as nn
import torch.nn.functional as F

import numpy as np

from pytorch3d.structures import Meshes
from pytorch3d.renderer import Materials, TensorProperties
from pytorch3d.common import Device
from pytorch3d.renderer.utils import TensorProperties
from kornia.color.rgb import linear_rgb_to_rgb

from pytorch3d.ops import interpolate_face_attributes
from pytorch3d.renderer.mesh.rasterizer import Fragments
from pytorch3d.io import load_obj
from pytorch3d.transforms import RotateAxisAngle
from pytorch3d.structures import Meshes
from pytorch3d.renderer import (
    look_at_view_transform,
    FoVPerspectiveCameras,
    RasterizationSettings,
    MeshRenderer,
    MeshRasterizer,
    TexturesVertex,
    FoVPerspectiveCameras,
    RasterizationSettings,
    MeshRenderer,
    MeshRasterizer,
    TexturesVertex,
    Materials,
)

from pytorch3d.vis.plotly_vis import plot_scene
import plotly.express as px
import wandb

from typing import Optional

class EnvironmentMap:
    def __init__(
        self,
        environment_map: torch.Tensor = None,
        directions: torch.Tensor = None,
        sineweight: torch.Tensor = None,
    ) -> None:
        self.directions = directions
        self.environment_map = environment_map * sineweight
        # self.environment_map = self.environment_map.squeeze(0)
        self.environment_map = self.environment_map

def blinn_phong_shading_env_map(
    device, meshes, fragments, envmap, cameras, materials, texels, kd, ks
) -> torch.Tensor:
    """
    Apply per pixel shading. First interpolate the vertex normals and
    vertex coordinates using the barycentric coordinates to get the position
    and normal at each pixel. Then compute the illumination for each pixel.
    Args:
        meshes: Batch of meshes
        fragments: Fragments named tuple with the outputs of rasterization
        envmap: lighting colours and lighting directions
        cameras: Cameras class containing a batch of cameras
        materials: Materials class containing a batch of material properties
    Returns:
        colors: (H, W, 3)
    """
    verts = meshes.verts_packed()  # (V, 3)
    faces = meshes.faces_packed()  # (F, 3)
    vertex_normals = meshes.verts_normals_packed()  # (V, 3)
    faces_verts = verts[faces]
    faces_normals = vertex_normals[faces]
    pixel_directions = interpolate_face_attributes(
        fragments.pix_to_face, fragments.bary_coords, faces_verts
    )  # (N, ..., 3) xyz coordinates of the points.
    pixel_normals = interpolate_face_attributes(
        fragments.pix_to_face, fragments.bary_coords, faces_normals
    )  # (N, ..., 3) xyz normal vectors for each point.
    light_directions = envmap.directions.to(
        device=device
    )  # (B, J, 3) unit vector associated with the direction of each pixel in a panoramic image where J = H*W
    light_colors = envmap.environment_map.to(
        device=device
    )  # (B, J, 3) RGB color of the environment map.
    camera_position = cameras.get_camera_center()
    shininess = materials.shininess.to(device=device)
    pixel_normals = (
        pixel_normals.squeeze(3).repeat(light_directions.shape[0], 1, 1, 1)
    )  # from (B, H, W, K, 3) -> (B, H, W, 3) assume K = 1 for now
    pixel_normals = F.normalize(pixel_normals, p=2, dim=-1, eps=1e-6)
    # create copies of light directions for batch matrix multiplication
    L_batch = light_directions.unsqueeze(1).repeat(1, pixel_normals.shape[1], 1, 1)  # (B, H, J, 3)
    # L_batch = torch.permute(L_batch, (0, 2, 1))  # (H, 3, J)
    # dot product between every image pixel and every light direction
    diffuse = torch.einsum("bhwk,bhjk->bhwj", pixel_normals, L_batch)  # (B, H, W, J)
    diffuse = torch.clamp(diffuse, min=0.0, max=1.0)                   
    # scale every dot product by colour of light source, prescaled by sineweight
    diffuse = torch.einsum("bjk,bhwj->bhwk", light_colors, diffuse)  # (B, H, W, 3)
    # create half-way vectors
    # view_directions = (camera_position - pixel_directions)
    # view_directions = view_directions.squeeze(3).repeat(light_directions.shape[0], 1, 1, 1) # (B, H, W, 3)
    # view_directions = F.normalize(view_directions, p=2, dim=-1, eps=1e-6)
    # view_directions = view_directions.unsqueeze(1).repeat(
    #     1, light_directions.shape[1], 1, 1, 1
    # )  # (B, J, H, W, 3)
    # view_directions = torch.permute(
    #     view_directions, (0, 2, 3, 1, 4)
    # )  # (B, H, W, J, 3)
    # # Half-way vectors between every pixels view-direction and all 'J' light directions
    # H = view_directions + light_directions.unsqueeze(1).unsqueeze(1)  # (B, H, W, J, 3) + (B, J, 3) -> (B, H, W, J, 3)
    # H = F.normalize(H, p=2, dim=-1, eps=1e-6)
    # dot product between every image pixel normal and every half-way vector
    #specular = torch.einsum("bhwk,bhwjk->bhwj", pixel_normals, H)  # (B, H, W, J)
    #specular = torch.clamp(specular, min=0.0, max=1.0)
    #specular = torch.pow(specular, shininess)
    # scale every dot product by colour of light source, prescaled by sineweight
    #specular = torch.einsum("bjk,bhwj->bhwk", light_colors, specular)  # (H, W, 3)
    # bp_specular_normalisation_factor = (shininess + 2) / (
    #     4 * (2 - torch.exp(-shininess / 2))
    # )
    # diffuse = diffuse.permute(0,3,1,2)
    # diffuse = linear_rgb_to_rgb(diffuse)
    # diffuse = diffuse.permute(0,2,3,1)
    colors = diffuse * texels
    return colors, diffuse, pixel_normals


class BlinnPhongShaderEnvMap(nn.Module):
    """
    Per pixel lighting - the lighting model is applied using the interpolated
    coordinates and normals for each pixel. The blending function returns the
    soft aggregated color using all the faces per pixel.
    """

    def __init__(
        self,
        device: Device = "cpu",
        cameras: Optional[TensorProperties] = None,
        envmap: EnvironmentMap = None,
        materials: Optional[Materials] = None,
        kd=None,
        ks=None,
    ) -> None:
        super().__init__()
        self.envmap = envmap
        self.materials = (
            materials if materials is not None else Materials(device=device)
        )
        self.cameras = cameras
        self.device = device
        self.kd = kd
        self.ks = ks

    def to(self, device: Device):
        # Manually move to device modules which are not subclasses of nn.Module
        cameras = self.cameras
        if cameras is not None:
            self.cameras = cameras.to(device)
        self.materials = self.materials.to(device)
        self.envmap = self.envmap.to(device)
        return self

    def forward(
        self, fragments: Fragments, meshes: Meshes, envmap: EnvironmentMap, **kwargs
    ) -> torch.Tensor:
        cameras = kwargs.get("cameras", self.cameras)
        if cameras is None:
            msg = "Cameras must be specified either at initialization \
                or in the forward pass of BlinnPhongShader"
            raise ValueError(msg)
        
        # fig = plot_scene({
        #     "scene_plot": {
        #         "mesh": meshes,
        #         "camera": cameras,
        #     },
        # })
        # #viewpoint_cameras=cameras)
        # wandb.log({'scene_plot': fig})

        texels = meshes.sample_textures(fragments).squeeze(3)
        envmap = envmap
        materials = kwargs.get("materials", self.materials)
        colors, diffuse, normals = blinn_phong_shading_env_map(
            device=self.device,
            meshes=meshes,
            fragments=fragments,
            envmap=envmap,
            cameras=cameras,
            materials=materials,
            texels=texels,
            kd=self.kd,
            ks=self.ks,
        )
        return colors, diffuse, texels, normals
    
class AlbedoEnvmap(nn.Module):
    """
    Per pixel lighting - the lighting model is applied using the interpolated
    coordinates and normals for each pixel. The blending function returns the
    soft aggregated color using all the faces per pixel.
    """

    def __init__(
        self,
        device: Device = "cpu",
        cameras: Optional[TensorProperties] = None,
        envmap: EnvironmentMap = None,
        materials: Optional[Materials] = None,
        kd=None,
        ks=None,
    ) -> None:
        super().__init__()
        self.envmap = envmap
        self.materials = (
            materials if materials is not None else Materials(device=device)
        )
        self.cameras = cameras
        self.device = device
        self.kd = kd
        self.ks = ks

    def to(self, device: Device):
        # Manually move to device modules which are not subclasses of nn.Module
        cameras = self.cameras
        if cameras is not None:
            self.cameras = cameras.to(device)
        self.materials = self.materials.to(device)
        self.envmap = self.envmap.to(device)
        return self

    def forward(
        self, fragments: Fragments, meshes: Meshes, envmap: EnvironmentMap, **kwargs
    ) -> torch.Tensor:
        cameras = kwargs.get("cameras", self.cameras)
        if cameras is None:
            msg = "Cameras must be specified either at initialization \
                or in the forward pass of BlinnPhongShader"
            raise ValueError(msg)

        texels = meshes.sample_textures(fragments).squeeze(3)
        envmap = envmap
        colors = texels
        return colors

def build_renderer_albedo(img_size, focal, kd, device):
    R, T = look_at_view_transform(10, 0, 0)  # camera's position
    cameras = FoVPerspectiveCameras(device=device, R=R, T=T, znear=0.01,
                                        zfar=50,
                                        fov=2*np.arctan(img_size//2/focal)*180./np.pi)

    raster_settings = RasterizationSettings(
        image_size=img_size,
        blur_radius=0.0,
        faces_per_pixel=1,
    )

    materials = Materials(shininess=500)

    ks = 0

    albedo_renderer = MeshRenderer(
        rasterizer=MeshRasterizer(cameras=cameras, raster_settings=raster_settings),
        shader=AlbedoEnvmap(
            device=device,
            cameras=cameras,
            envmap=None,
            materials=materials,
            kd=kd,
            ks=ks,
        ),
    )
    return albedo_renderer

def build_renderer(img_size, focal, kd, device):
    R, T = look_at_view_transform(10, 0, 0)  # camera's position
    cameras = FoVPerspectiveCameras(device=device, R=R, T=T, znear=0.01,
                                        zfar=50,
                                        fov=2*np.arctan(img_size//2/focal)*180./np.pi)

    raster_settings = RasterizationSettings(
        image_size=img_size,
        blur_radius=0.0,
        faces_per_pixel=1,
        #max_faces_per_bin=100000,
    )

    materials = Materials(shininess=500)

    ks = 1 - kd

    #wandb.init(project="3DMM")

    blinn_phong_envmap_renderer = MeshRenderer(
        rasterizer=MeshRasterizer(cameras=cameras, raster_settings=raster_settings),
        shader=BlinnPhongShaderEnvMap(
            device=device,
            cameras=cameras,
            envmap=None,
            materials=materials,
            kd=kd,
            ks=ks,
        ),
    )
    return blinn_phong_envmap_renderer