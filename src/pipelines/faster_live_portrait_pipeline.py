# -*- coding: utf-8 -*-
# @Author  : wenshao
# @Email   : wenshaoguo0611@gmail.com
# @Project : FasterLivePortrait
# @FileName: faster_live_portrait_pipeline.py

import copy

import cv2

import numpy as np

from .. import models
from ..utils.crop import crop_image, parse_bbox_from_landmark, crop_image_by_bbox, paste_back
from ..utils.utils import resize_to_limit, prepare_paste_back, get_rotation_matrix, calc_lip_close_ratio, \
    calc_eye_close_ratio, transform_keypoint, concat_feat


class FasterLivePortraitPipeline:
    def __init__(self, cfg, **kwargs):
        self.cfg = cfg
        self.init(**kwargs)

    def init(self, **kwargs):
        self.init_models(**kwargs)
        self.init_vars(**kwargs)

    def init_models(self, **kwargs):
        self.model_dict = {}
        for model_name in self.cfg.models:
            print(f"loading model: {model_name}")
            print(self.cfg.models[model_name])
            self.model_dict[model_name] = getattr(models, self.cfg.models[model_name]["name"])(
                **self.cfg.models[model_name])

    def init_vars(self, **kwargs):
        self.mask_crop = cv2.imread(self.cfg.infer_params.mask_crop_path, cv2.IMREAD_COLOR)
        self.crop_infos = []
        self.src_infos = []
        self.R_d_0 = None
        self.x_d_0_info = None
        self.frame_id = 0
        self.src_lmk_pre = None
        self.R_d_0 = None
        self.x_d_0_info = None
        self.src_img_path = None
        self.src_img = None

    def calc_combined_eye_ratio(self, c_d_eyes_i, source_lmk):
        c_s_eyes = calc_eye_close_ratio(source_lmk[None])
        c_d_eyes_i = np.array(c_d_eyes_i).reshape(1, 1)
        # [c_s,eyes, c_d,eyes,i]
        combined_eye_ratio_tensor = np.concatenate([c_s_eyes, c_d_eyes_i], axis=1)
        return combined_eye_ratio_tensor

    def calc_combined_lip_ratio(self, c_d_lip_i, source_lmk):
        c_s_lip = calc_lip_close_ratio(source_lmk[None])
        c_d_lip_i = np.array(c_d_lip_i).reshape(1, 1)  # 1x1
        # [c_s,lip, c_d,lip,i]
        combined_lip_ratio_tensor = np.concatenate([c_s_lip, c_d_lip_i], axis=1)  # 1x2
        return combined_lip_ratio_tensor

    def prepare_src_image(self, src_img_path, **kwargs):
        """
        prepare source image
        """
        self.src_img_path = src_img_path
        img_bgr = cv2.imread(src_img_path, cv2.IMREAD_COLOR)
        img_bgr = resize_to_limit(img_bgr, self.cfg.infer_params.source_max_dim, self.cfg.infer_params.source_division)
        img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        self.src_img = img_rgb.copy()
        h, w = img_rgb.shape[:2]
        src_faces = self.model_dict["face_analysis"].predict(img_bgr)

        if len(src_faces) == 0:
            print("No face detected in the source image.")
            return None

        # 如果是实时，只关注一张人脸
        if kwargs.get("realtime", False):
            src_faces = src_faces[:1]

        crop_infos = []
        for i in range(len(src_faces)):
            # NOTE: temporarily only pick the first face, to support multiple face in the future
            src_face = src_faces[i]
            lmk = src_face.landmark  # this is the 106 landmarks from insightface
            # crop the face
            ret_dct = crop_image(
                img_rgb,  # ndarray
                lmk,  # 106x2 or Nx2
                dsize=self.cfg.crop_params.src_dsize,
                scale=self.cfg.crop_params.src_scale,
                vx_ratio=self.cfg.crop_params.src_vx_ratio,
                vy_ratio=self.cfg.crop_params.src_vy_ratio,
            )
            lmk = self.model_dict["landmark"].predict(img_rgb, lmk)
            ret_dct["lmk_crop"] = lmk

            # update a 256x256 version for network input
            ret_dct["img_crop_256x256"] = cv2.resize(
                ret_dct["img_crop"], (256, 256), interpolation=cv2.INTER_AREA
            )
            ret_dct["lmk_crop_256x256"] = ret_dct["lmk_crop"] * 256 / self.cfg.crop_params.src_dsize
            crop_infos.append(ret_dct)

        self.crop_infos = crop_infos[:]
        src_infos = [[] for _ in range(len(crop_infos))]
        for i, crop_info in enumerate(crop_infos):
            source_lmk = crop_info['lmk_crop']
            img_crop, img_crop_256x256 = crop_info['img_crop'], crop_info['img_crop_256x256']
            pitch, yaw, roll, t, exp, scale, kp = self.model_dict["motion_extractor"].predict(
                img_crop_256x256)
            x_s_info = {
                "pitch": pitch,
                "yaw": yaw,
                "roll": roll,
                "t": t,
                "exp": exp,
                "scale": scale,
                "kp": kp
            }
            src_infos[i].append(copy.deepcopy(x_s_info))
            x_c_s = kp
            R_s = get_rotation_matrix(pitch, yaw, roll)
            f_s = self.model_dict["app_feat_extractor"].predict(img_crop_256x256)
            x_s = transform_keypoint(pitch, yaw, roll, t, exp, scale, kp)
            src_infos[i].extend([source_lmk.copy(), R_s.copy(), f_s.copy(), x_s.copy(), x_c_s.copy()])
            flag_lip_zero = self.cfg.infer_params.flag_lip_zero  # not overwrite
            if flag_lip_zero:
                # let lip-open scalar to be 0 at first
                c_d_lip_before_animation = [0.]
                combined_lip_ratio_tensor_before_animation = self.calc_combined_lip_ratio(
                    c_d_lip_before_animation, source_lmk)
                if combined_lip_ratio_tensor_before_animation[0][0] < self.cfg.infer_params.lip_zero_threshold:
                    flag_lip_zero = False
                    src_infos[i].append(None)
                    src_infos[i].append(flag_lip_zero)
                else:
                    lip_delta_before_animation = self.model_dict['stitching_lip_retarget'].predict(
                        concat_feat(x_s, combined_lip_ratio_tensor_before_animation))
                    src_infos[i].append(lip_delta_before_animation.copy())
                    src_infos[i].append(flag_lip_zero)

            ######## prepare for pasteback ########
            if self.cfg.infer_params.flag_pasteback and self.cfg.infer_params.flag_do_crop and self.cfg.infer_params.flag_stitching:
                mask_ori_float = prepare_paste_back(self.mask_crop, crop_info['M_c2o'],
                                                    dsize=(img_rgb.shape[1], img_rgb.shape[0]))
                src_infos[i].append(mask_ori_float.copy())
            else:
                src_infos[i].append(None)
        self.src_infos = src_infos[:]
        return img_rgb

    def retarget_eye(self, kp_source, eye_close_ratio):
        """
        kp_source: BxNx3
        eye_close_ratio: Bx3
        Return: Bx(3*num_kp+2)
        """
        feat_eye = concat_feat(kp_source, eye_close_ratio)
        delta = self.model_dict['stitching_eye_retarget'].predict(feat_eye)
        return delta

    def retarget_lip(self, kp_source, lip_close_ratio):
        """
        kp_source: BxNx3
        lip_close_ratio: Bx2
        """
        feat_lip = concat_feat(kp_source, lip_close_ratio)
        delta = self.model_dict['stitching_lip_retarget'].predict(feat_lip)
        return delta

    def stitching(self, kp_source, kp_driving):
        """ conduct the stitching
        kp_source: Bxnum_kpx3
        kp_driving: Bxnum_kpx3
        """

        bs, num_kp = kp_source.shape[:2]

        kp_driving_new = kp_driving.copy()

        delta = self.model_dict['stitching'].predict(concat_feat(kp_source, kp_driving_new))

        delta_exp = delta[..., :3 * num_kp].reshape(bs, num_kp, 3)  # 1x20x3
        delta_tx_ty = delta[..., 3 * num_kp:3 * num_kp + 2].reshape(bs, 1, 2)  # 1x1x2

        kp_driving_new += delta_exp
        kp_driving_new[..., :2] += delta_tx_ty

        return kp_driving_new

    def run(self, image, img_src, **kwargs):
        img_bgr = image
        img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        I_p_pstbk = img_src.copy()
        realtime = kwargs.get("realtime", False)

        if self.cfg.infer_params.flag_crop_driving_video:
            if self.src_lmk_pre is None:
                src_face = self.model_dict["face_analysis"].predict(img_bgr)
                if len(src_face) == 0:
                    self.src_lmk_pre = None
                    return None, None, None
                src_face = src_face[0]
                lmk = src_face.landmark
                lmk = self.model_dict["landmark"].predict(img_rgb, lmk)
                self.src_lmk_pre = lmk.copy()
            else:
                lmk = self.model_dict["landmark"].predict(img_rgb, self.src_lmk_pre)
                self.src_lmk_pre = lmk.copy()

            ret_bbox = parse_bbox_from_landmark(
                lmk,
                scale=self.cfg.crop_params.dri_scale,
                vx_ratio_crop_video=self.cfg.crop_params.dri_vx_ratio,
                vy_ratio=self.cfg.crop_params.dri_vy_ratio,
            )["bbox"]
            global_bbox = [
                ret_bbox[0, 0],
                ret_bbox[0, 1],
                ret_bbox[2, 0],
                ret_bbox[2, 1],
            ]
            ret_dct = crop_image_by_bbox(
                img_rgb,
                global_bbox,
                lmk=lmk,
                dsize=kwargs.get("dsize", 512),
                flag_rot=False,
                borderValue=(0, 0, 0),
            )
            lmk_crop = ret_dct["lmk_crop"]
            img_crop = ret_dct["img_crop"]
            img_crop = cv2.resize(img_crop, (256, 256))
        else:
            if self.src_lmk_pre is None:
                src_face = self.model_dict["face_analysis"].predict(img_bgr)
                if len(src_face) == 0:
                    self.src_lmk_pre = None
                    return None, None, None
                src_face = src_face[0]
                lmk = src_face.landmark
                lmk = self.model_dict["landmark"].predict(img_rgb, lmk)
                self.src_lmk_pre = lmk.copy()
            else:
                lmk = self.model_dict["landmark"].predict(img_rgb, self.src_lmk_pre)
                self.src_lmk_pre = lmk.copy()
            lmk_crop = lmk.copy()
            img_crop = cv2.resize(img_rgb, (256, 256))

        input_eye_ratio = calc_eye_close_ratio(lmk_crop[None])
        input_lip_ratio = calc_lip_close_ratio(lmk_crop[None])
        pitch, yaw, roll, t, exp, scale, kp = self.model_dict["motion_extractor"].predict(img_crop)
        x_d_i_info = {
            "pitch": pitch,
            "yaw": yaw,
            "roll": roll,
            "t": t,
            "exp": exp,
            "scale": scale,
            "kp": kp
        }
        R_d_i = get_rotation_matrix(pitch, yaw, roll)

        if self.R_d_0 is None:
            self.R_d_0 = R_d_i.copy()
            self.x_d_0_info = copy.deepcopy(x_d_i_info)
        R_d_0 = self.R_d_0.copy()
        x_d_0_info = copy.deepcopy(self.x_d_0_info)
        out_crop, out_org = None, None
        for j in range(len(self.src_infos)):
            x_s_info, source_lmk, R_s, f_s, x_s, x_c_s, lip_delta_before_animation, flag_lip_zero, mask_ori_float = \
                self.src_infos[j]
            if self.cfg.infer_params.flag_relative_motion:
                R_new = (R_d_i @ np.transpose(R_d_0, (0, 2, 1))) @ R_s
                delta_new = x_s_info['exp'] + (x_d_i_info['exp'] - x_d_0_info['exp'])
                scale_new = x_s_info['scale'] * (x_d_i_info['scale'] / x_d_0_info['scale'])
                t_new = x_s_info['t'] + (x_d_i_info['t'] - x_d_0_info['t'])
            else:
                R_new = R_d_i
                delta_new = x_d_i_info['exp'].copy()
                scale_new = x_s_info['scale'].copy()
                t_new = x_d_i_info['t'].copy()

            t_new[..., 2] = 0  # zero tz
            x_d_i_new = scale_new * (x_c_s @ R_new + delta_new) + t_new
            # Algorithm 1:
            if not self.cfg.infer_params.flag_stitching and not self.cfg.infer_params.flag_eye_retargeting and not self.cfg.infer_params.flag_lip_retargeting:
                # without stitching or retargeting
                if flag_lip_zero:
                    x_d_i_new += lip_delta_before_animation.reshape(-1, x_s.shape[1], 3)
                else:
                    pass
            elif self.cfg.infer_params.flag_stitching and not self.cfg.infer_params.flag_eye_retargeting and not self.cfg.infer_params.flag_lip_retargeting:
                # with stitching and without retargeting
                if flag_lip_zero:
                    x_d_i_new = self.stitching(x_s, x_d_i_new) + lip_delta_before_animation.reshape(
                        -1, x_s.shape[1], 3)
                else:
                    x_d_i_new = self.stitching(x_s, x_d_i_new)
            else:
                eyes_delta, lip_delta = None, None
                if self.cfg.infer_params.flag_eye_retargeting:
                    c_d_eyes_i = input_eye_ratio
                    combined_eye_ratio_tensor = self.calc_combined_eye_ratio(c_d_eyes_i,
                                                                             source_lmk)
                    # ∆_eyes,i = R_eyes(x_s; c_s,eyes, c_d,eyes,i)
                    eyes_delta = self.retarget_eye(x_s, combined_eye_ratio_tensor)
                if self.cfg.infer_params.flag_lip_retargeting:
                    c_d_lip_i = input_lip_ratio
                    combined_lip_ratio_tensor = self.calc_combined_lip_ratio(c_d_lip_i, source_lmk)
                    # ∆_lip,i = R_lip(x_s; c_s,lip, c_d,lip,i)
                    lip_delta = self.retarget_lip(x_s, combined_lip_ratio_tensor)

                if self.cfg.infer_params.flag_relative_motion:  # use x_s
                    x_d_i_new = x_s + \
                                (eyes_delta.reshape(-1, x_s.shape[1], 3) if eyes_delta is not None else 0) + \
                                (lip_delta.reshape(-1, x_s.shape[1], 3) if lip_delta is not None else 0)
                else:  # use x_d,i
                    x_d_i_new = x_d_i_new + \
                                (eyes_delta.reshape(-1, x_s.shape[1], 3) if eyes_delta is not None else 0) + \
                                (lip_delta.reshape(-1, x_s.shape[1], 3) if lip_delta is not None else 0)

                if self.cfg.infer_params.flag_stitching:
                    x_d_i_new = self.stitching(x_s, x_d_i_new)

            out_crop = self.model_dict["warping_spade"].predict(f_s, x_s, x_d_i_new)
            if not realtime and self.cfg.infer_params.flag_pasteback and self.cfg.infer_params.flag_do_crop and self.cfg.infer_params.flag_stitching:
                # TODO: pasteback is slow, considering optimize it using multi-threading or GPU
                I_p_pstbk = paste_back(out_crop, self.crop_infos[j]['M_c2o'], I_p_pstbk, mask_ori_float)

        return img_crop, out_crop, I_p_pstbk
