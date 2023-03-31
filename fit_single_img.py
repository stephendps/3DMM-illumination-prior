from facenet_pytorch import MTCNN
from core.RENISetup import RENI
from RENI.src.utils.loss_functions import RENITestLossInverse
from RENI.src.models.RENI import SO2InvariantRepresentation
from RENI.src.utils.utils import sRGB
from RENI.src.utils.utils import sRGB_old
from RENI.src.utils.pytorch3d_envmap_shader import EnvironmentMap
from core.options import ImageFittingOptions
import cv2
import face_alignment
import numpy as np
from core import get_recon_model
import os
import torch
import core.utils as utils
from tqdm import tqdm
import core.losses as losses
import matplotlib.pyplot as plt
from PIL import Image

from torch.utils.tensorboard import SummaryWriter
import wandb

import os
os.environ['CUDA_LAUNCH_BLOCKING'] = "1"
#os.environ["PYTORCH_CUDA_ALLOC_CONF"]


def fit(args):
    writer = SummaryWriter()
    #wandb.init(project="3DMM",
    #    notes="playing with cameras")

    # init face detection and lms detection models
    print('loading models')
    mtcnn = MTCNN(device=args.device, select_largest=False)
    fa = face_alignment.FaceAlignment(
        face_alignment.LandmarksType._3D, flip_input=False)
    recon_model = get_recon_model(model=args.recon_model,
                                  device=args.device,
                                  batch_size=1,
                                  img_size=args.tar_size)
    reni = RENI(64, 128, device=args.device)
    reni_model = reni.model

    print('loading images')
    img_arr = cv2.imread(args.img_path)[:, :, ::-1]
    orig_h, orig_w = img_arr.shape[:2]

    print('image is loaded. width: %d, height: %d' % (orig_w, orig_h))

    # detect the face using MTCNN
    bboxes, probs = mtcnn.detect(img_arr)

    if bboxes is None:
        print('no face detected')
    else:
        bbox = utils.pad_bbox(bboxes[0], (orig_w, orig_h), args.padding_ratio)
        face_w = bbox[2] - bbox[0]
        face_h = bbox[3] - bbox[1]
        assert face_w == face_h
    print('A face is detected. l: %d, t: %d, r: %d, b: %d'
          % (bbox[0], bbox[1], bbox[2], bbox[3]))

    face_img = img_arr[bbox[1]:bbox[3], bbox[0]:bbox[2], :]
    resized_face_img = cv2.resize(face_img, (args.tar_size, args.tar_size))

    lms = fa.get_landmarks_from_image(resized_face_img)[0]
    lms = lms[:, :2][None, ...]
    lms = torch.tensor(lms, dtype=torch.float32, device=args.device)
    img_tensor = torch.tensor(
        resized_face_img[None, ...], dtype=torch.float32, device=args.device)
    img_tensor = img_tensor / 255

    print('landmarks detected.')

    lm_weights = utils.get_lm_weights(args.device)
    print('start rigid fitting')
    rigid_optimizer = torch.optim.Adam([recon_model.get_rot_tensor(),
                                        recon_model.get_trans_tensor()],
                                       lr=args.rf_lr)
    for i in tqdm(range(args.first_rf_iters)):
        rigid_optimizer.zero_grad()
        pred_dict = recon_model(recon_model.get_packed_tensors(), render=False)
        lm_loss_val = losses.lm_loss(
            pred_dict['lms_proj'], lms, lm_weights, img_size=args.tar_size)
        
        orig_face_log = (img_tensor*255).cpu().numpy().squeeze().astype(np.uint8)
        # fig, ax = plt.subplots(figsize=(4, 4))
        # ax.imshow(orig_face_log)
        # ax.axis('off')

        # for landmark in lms.cpu():
        #     #ax.scatter(*np.meshgrid([bbox[0], bbox[2]], [bbox[1], bbox[3]]))
        #     ax.scatter(landmark[:, 0], landmark[:, 1], s=8)
        # writer.add_figure('landmarks_rigid', fig, global_step=i)

        total_loss = args.lm_loss_w * lm_loss_val
        total_loss.backward()
        rigid_optimizer.step()
    print('done rigid fitting. lm_loss: %f' %
          lm_loss_val.detach().cpu().numpy())
    print('start non-rigid fitting')
    #here - get_gamma_tensor
    nonrigid_optimizer = torch.optim.Adam([
        {'params': [recon_model.get_id_tensor(), recon_model.get_exp_tensor(),
         recon_model.get_tex_tensor(), recon_model.get_rot_tensor(), 
         recon_model.get_trans_tensor()]},
         {'params': reni_model.parameters(), 'lr': 1e-1}], lr=args.nrf_lr)
    #here add reni lr into args later
    #reni_optimizer = torch.optim.Adam(reni_model.parameters(), lr=1e-1)
    reni_criterion = RENITestLossInverse(alpha=1e-7, beta=1e-1)
    #for i in tqdm(range(args.first_nrf_iters)):
    for i in tqdm(range(args.first_nrf_iters)):
        nonrigid_optimizer.zero_grad()

        D = reni.directions.repeat(img_tensor.shape[0], 1, 1).type_as(img_tensor)
        S = reni.sineweight.repeat(img_tensor.shape[0], 1, 1).type_as(img_tensor)

        #reni stuff
        Z = reni_model.mu # get latent code

        #reni_input = SO2InvariantRepresentation(Z, reni.directions.to(device=args.device))
        #reni_output = reni_model(reni_input)
        reni_output = reni_model(Z, D)
        reni_output = reni.unnormalise(reni_output)

        envmap = EnvironmentMap(
            environment_map=reni_output,
            directions=D,
            sineweight=S
        )

        envmap_im = reni_output.view(-1, reni.H, reni.W, 3)
        envmap_im = sRGB_old(envmap_im)

        #envmap_im = Image.fromarray(envmap_im)  
        #wandb.log({"envmap": wandb.Image(envmap_im)}
        writer.add_image('envmap', envmap_im.squeeze(), global_step=i, dataformats='HWC')

        pred_dict = recon_model(recon_model.get_packed_tensors(), envmap=envmap, 
                                render=True)
        rendered_img = pred_dict['rendered_img']
        #rendered_img = reni.to_sRGB(rendered_img, args.tar_size, args.tar_size)#########
        lms_proj = pred_dict['lms_proj']
        #face_color not used in losses? - can omit?
        face_texture = pred_dict['face_texture']

        mask = torch.sum(rendered_img, 3).detach()


        ### LOGGING ###
        render_out = (rendered_img*255).detach().cpu().numpy().squeeze().astype(np.uint8)

        lm_out = lms.cpu().numpy()
        lm_proj_out = lms_proj.detach().cpu().numpy()
        fig, ax = plt.subplots(figsize=(4, 4))
        ax.axis('off')
        ax.imshow(render_out)
        #ax.scatter(*np.meshgrid([bbox[0], bbox[2]], [bbox[1], bbox[3]]))
        ax.scatter(lm_out[:,:, 0], lm_out[:,:, 1], s=8)
        #ax.scatter(*np.meshgrid([bbox[0], bbox[2]], [bbox[1], bbox[3]]))
        ax.scatter(lm_proj_out[:,:, 0], lm_proj_out[:,:, 1], s=8, color='r')
        #wandb.log({'landmarks_proj': fig})
        writer.add_figure('landmarks_proj', fig, global_step=i)
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(4, 4))
        ax.axis('off')
        ax.imshow(orig_face_log)
        #ax.scatter(*np.meshgrid([bbox[0], bbox[2]], [bbox[1], bbox[3]]))
        ax.scatter(lm_proj_out[:,:, 0], lm_proj_out[:,:, 1], s=8)
        #wandb.log({'landmarks': fig})
        writer.add_figure('landmarks', fig, global_step=i)
        plt.close(fig)

        rendered_img_albedo = pred_dict['albedo_img'].permute(3,1,2,0)*255
        render_out_albedo = rendered_img_albedo.detach().cpu().numpy().squeeze().astype(np.uint8)
        writer.add_image('albedo', render_out_albedo, global_step=i)

        rendered_img_lighting = pred_dict['lighting_img']
        #rendered_img_lighting = reni.to_sRGB(rendered_img_lighting, args.tar_size, args.tar_size)
        render_out_lighting = rendered_img_lighting.permute(3,1,2,0)*255
        render_out_lighting = render_out_lighting.detach().cpu().numpy().squeeze().astype(np.uint8)
        writer.add_image('lighting', render_out_lighting, global_step=i)

        normal_map = pred_dict['normals'].detach().cpu().numpy().squeeze()
        fig, ax = plt.subplots(figsize=(4, 4))
        ax.axis('off')
        ax.imshow(0.5*(normal_map+1))
        writer.add_figure('normals', fig, global_step=i)
        plt.close(fig)
        #######

        #reni_loss, _, _, _ = reni_criterion(rendered_img, img_tensor, Z)

        photo_loss_val = losses.photo_loss(
            rendered_img, img_tensor, mask > 0) * args.rgb_loss_w
            #rendered_img, img_tensor)
        
        lm_loss_val = losses.lm_loss(lms_proj, lms, lm_weights,
                                     img_size=args.tar_size) * args.lm_loss_w
        id_reg_loss = losses.get_l2(recon_model.get_id_tensor()) * args.id_reg_w
        exp_reg_loss = losses.get_l2(recon_model.get_exp_tensor()) * args.exp_reg_w
        tex_reg_loss = losses.get_l2(recon_model.get_tex_tensor()) * args.tex_reg_w
        reni_reg_loss = losses.get_l2(Z) * args.reni_reg_w
        #here think about tex_reg? - later
        tex_loss_val = losses.reflectance_loss(
            face_texture, recon_model.get_skinmask()) * args.tex_w

        loss = lm_loss_val + \
            id_reg_loss + \
            exp_reg_loss + \
            tex_reg_loss + \
            tex_loss_val + \
            photo_loss_val + \
            reni_reg_loss

        writer.add_scalars('losses', {'lm':lm_loss_val,
                                      'id_reg':id_reg_loss,
                                      'exp_reg':exp_reg_loss,
                                      'tex_reg':tex_reg_loss,
                                      'reni_reg':reni_reg_loss,
                                      'tex_loss':tex_loss_val,
                                      'photo_loss':photo_loss_val}, global_step=i)

        loss.backward()
        #print(Z.grad)
        nonrigid_optimizer.step()

    loss_str = ''
    loss_str += 'lm_loss: %f\t' % lm_loss_val.detach().cpu().numpy()
    loss_str += 'photo_loss: %f\t' % photo_loss_val.detach().cpu().numpy()
    loss_str += 'tex_loss: %f\t' % tex_loss_val.detach().cpu().numpy()
    loss_str += 'id_reg_loss: %f\t' % id_reg_loss.detach().cpu().numpy()
    loss_str += 'exp_reg_loss: %f\t' % exp_reg_loss.detach().cpu().numpy()
    loss_str += 'tex_reg_loss: %f\t' % tex_reg_loss.detach().cpu().numpy()
    loss_str += 'reni_reg_loss: %f\t' % reni_reg_loss.detach().cpu().numpy()
    print('done non rigid fitting.', loss_str)

    with torch.no_grad():
        coeffs = recon_model.get_packed_tensors()
        pred_dict = recon_model(coeffs, envmap=envmap, render=True)
        rendered_img = pred_dict['rendered_img']
        #rendered_img = reni.to_sRGB(rendered_img, args.tar_size, args.tar_size)
        mask_img = torch.sum(rendered_img, 3).cpu().numpy().squeeze()
        rendered_img = rendered_img.cpu().numpy().squeeze()
        #out_img = rendered_img[:, :, :3].astype(np.uint8)
        out_img = (rendered_img*255).astype(np.uint8)
        out_mask = (mask_img > 0).astype(np.uint8)
        resized_out_img = cv2.resize(out_img, (face_w, face_h))
        resized_mask = cv2.resize(
            out_mask, (face_w, face_h), cv2.INTER_NEAREST)[..., None]

        composed_img = img_arr.copy()
        composed_img = composed_img.astype(np.uint8)
        composed_face = composed_img[bbox[1]:bbox[3], bbox[0]:bbox[2], :] * \
            (1 - resized_mask) + resized_out_img * resized_mask
        composed_img[bbox[1]:bbox[3], bbox[0]:bbox[2], :] = composed_face

        #composed_img = np.floor(composed_img*256)

        utils.mymkdirs(args.res_folder)
        basename = os.path.basename(args.img_path)[:-4]
        # save the composed image
        out_composed_img_path = os.path.join(
            args.res_folder, basename + '_composed_img.jpg')
        cv2.imwrite(out_composed_img_path, composed_img[:, :, ::-1])
        # save the coefficients
        out_coeff_path = os.path.join(
            args.res_folder, basename + '_coeffs.npy')
        np.save(out_coeff_path,
                coeffs.detach().cpu().numpy().squeeze())

        # save the mesh into obj format
        out_obj_path = os.path.join(
            args.res_folder, basename+'_mesh.obj')
        vs = pred_dict['vs'].cpu().numpy().squeeze()
        tri = pred_dict['tri'].cpu().numpy().squeeze()
        color = pred_dict['color'].cpu().numpy().squeeze()
        utils.save_obj(out_obj_path, vs, tri+1, color)


        print('composed image is saved at %s' % args.res_folder)
        writer.close()


if __name__ == '__main__':
    args = ImageFittingOptions()
    args = args.parse()
    args.device = 'cuda:%d' % args.gpu
    #torch.autograd.set_detect_anomaly(True)
    fit(args)
