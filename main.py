import cv2
import torch
import pyvirtualcam
import numpy as np
import mediapipe as mp
from PIL import Image

import tha2.poser.modes.mode_20_wx
from models import TalkingAnimeLight
from pose import get_pose
from utils import preprocessing_image, postprocessing_image

import errno
import json
import os
import queue
import socket
import time
import math
import re
from collections import OrderedDict
from multiprocessing import Value, Process, Queue

from pyanime4k import ac

from tha2.mocap.ifacialmocap_constants import *

from args import args

device = torch.device('cuda') if torch.cuda.is_available() and not args.skip_model else torch.device('cpu')


def create_default_blender_data():
    data = {}

    for blendshape_name in BLENDSHAPE_NAMES:
        data[blendshape_name] = 0.0

    data[HEAD_BONE_X] = 0.0
    data[HEAD_BONE_Y] = 0.0
    data[HEAD_BONE_Z] = 0.0
    data[HEAD_BONE_QUAT] = [0.0, 0.0, 0.0, 1.0]

    data[LEFT_EYE_BONE_X] = 0.0
    data[LEFT_EYE_BONE_Y] = 0.0
    data[LEFT_EYE_BONE_Z] = 0.0
    data[LEFT_EYE_BONE_QUAT] = [0.0, 0.0, 0.0, 1.0]

    data[RIGHT_EYE_BONE_X] = 0.0
    data[RIGHT_EYE_BONE_Y] = 0.0
    data[RIGHT_EYE_BONE_Z] = 0.0
    data[RIGHT_EYE_BONE_QUAT] = [0.0, 0.0, 0.0, 1.0]

    return data


ifm_converter = tha2.poser.modes.mode_20_wx.IFacialMocapPoseConverter20()


class IFMClientProcess(Process):
    def __init__(self):
        super().__init__()
        self.queue = Queue()
        self.should_terminate = Value('b', False)
        self.address = args.ifm.split(':')[0]
        self.port = int(args.ifm.split(':')[1])
        self.perf_time = 0

    def run(self):

        udpClntSock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        data = "iFacialMocap_sahuasouryya9218sauhuiayeta91555dy3719"

        data = data.encode('utf-8')

        udpClntSock.sendto(data, (self.address, self.port))

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setblocking(False)
        self.socket.bind(("", self.port))
        self.socket.settimeout(0.1)
        while True:
            if self.should_terminate.value:
                break
            try:
                socket_bytes = self.socket.recv(8192)
            except socket.error as e:
                err = e.args[0]
                if err == errno.EAGAIN or err == errno.EWOULDBLOCK or err == 'timed out':
                    continue
                else:
                    raise e
            socket_string = socket_bytes.decode("utf-8")
            # print(socket_string)
            # blender_data = json.loads(socket_string)
            data = self.convert_from_blender_data(socket_string)
            # cur_time = time.perf_counter()
            # fps = 1 / (cur_time - self.perf_time)
            # self.perf_time = cur_time
            # print(fps)
            try:
                self.queue.put_nowait(data)
            except queue.Full:
                pass
        self.queue.close()
        self.socket.close()

    @staticmethod
    def convert_from_blender_data(blender_data):
        data = {}

        for item in blender_data.split('|'):
            if item.find('#') != -1:
                k, arr = item.split('#')
                arr = [float(n) for n in arr.split(',')]
                data[k.replace("_L", "Left").replace("_R", "Right")] = arr
            elif item.find('-') != -1:
                k, v = item.split("-")
                data[k.replace("_L", "Left").replace("_R", "Right")] = float(v) / 100

        to_rad = 57.3
        data[HEAD_BONE_X] = data["=head"][0] / to_rad
        data[HEAD_BONE_Y] = data["=head"][1] / to_rad
        data[HEAD_BONE_Z] = data["=head"][2] / to_rad
        data[HEAD_BONE_QUAT] = [data["=head"][3], data["=head"][4], data["=head"][5], 1]
        # print(data[HEAD_BONE_QUAT][2],min(data[EYE_BLINK_LEFT],data[EYE_BLINK_RIGHT]))
        data[RIGHT_EYE_BONE_X] = data["rightEye"][0] / to_rad
        data[RIGHT_EYE_BONE_Y] = data["rightEye"][1] / to_rad
        data[RIGHT_EYE_BONE_Z] = data["rightEye"][2] / to_rad
        data[LEFT_EYE_BONE_X] = data["leftEye"][0] / to_rad
        data[LEFT_EYE_BONE_Y] = data["leftEye"][1] / to_rad
        data[LEFT_EYE_BONE_Z] = data["leftEye"][2] / to_rad

        return data


class ModelClientProcess(Process):
    def __init__(self, input_image):
        super().__init__()
        self.should_terminate = Value('b', False)
        self.updated = Value('b', False)
        self.data = None
        self.input_image = input_image
        self.output_queue = Queue()
        self.input_queue = Queue()

    def run(self):
        model = None
        if not args.skip_model:
            model = TalkingAnimeLight().to(device)
            model = model.eval()
            model = model
            print("Pretrained Model Loaded")

        mouth_eye_vector = torch.empty(1, 27)
        pose_vector = torch.empty(1, 3)

        input_image = self.input_image.to(device)
        mouth_eye_vector = mouth_eye_vector.to(device)
        pose_vector = pose_vector.to(device)

        model_cache = OrderedDict()
        tot = 0
        hit = 0

        while True:
            model_input = None
            try:
                while not self.input_queue.empty():
                    model_input = self.input_queue.get_nowait()
            except queue.Empty:
                continue
            if model_input is None: continue
            simplify_arr = [1000] * ifm_converter.pose_size
            if args.simplify >= 1:
                simplify_arr = [200] * ifm_converter.pose_size
                simplify_arr[ifm_converter.eye_wink_left_index] = 50
                simplify_arr[ifm_converter.eye_wink_right_index] = 50
                simplify_arr[ifm_converter.eye_happy_wink_left_index] = 50
                simplify_arr[ifm_converter.eye_happy_wink_right_index] = 50
                simplify_arr[ifm_converter.eye_surprised_left_index] = 30
                simplify_arr[ifm_converter.eye_surprised_right_index] = 30
                simplify_arr[ifm_converter.iris_rotation_x_index] = 25
                simplify_arr[ifm_converter.iris_rotation_y_index] = 25
                simplify_arr[ifm_converter.eye_raised_lower_eyelid_left_index] = 10
                simplify_arr[ifm_converter.eye_raised_lower_eyelid_right_index] = 10
                simplify_arr[ifm_converter.mouth_lowered_corner_left_index] = 5
                simplify_arr[ifm_converter.mouth_lowered_corner_right_index] = 5
                simplify_arr[ifm_converter.mouth_raised_corner_left_index] = 5
                simplify_arr[ifm_converter.mouth_raised_corner_right_index] = 5
            if args.simplify >= 2:
                simplify_arr[ifm_converter.head_x_index]=100
                simplify_arr[ifm_converter.head_y_index]=100
                simplify_arr[ifm_converter.eye_surprised_left_index] = 10
                simplify_arr[ifm_converter.eye_surprised_right_index] = 10
                model_input[ifm_converter.eye_wink_left_index - 12] += model_input[
                    ifm_converter.eye_happy_wink_left_index - 12]
                model_input[ifm_converter.eye_happy_wink_left_index - 12] = model_input[
                                                                                ifm_converter.eye_wink_left_index - 12] / 2
                model_input[ifm_converter.eye_wink_left_index - 12] = model_input[
                                                                          ifm_converter.eye_wink_left_index - 12] / 2
                model_input[ifm_converter.eye_wink_right_index - 12] += model_input[
                    ifm_converter.eye_happy_wink_right_index - 12]
                model_input[ifm_converter.eye_happy_wink_right_index - 12] = model_input[
                                                                                 ifm_converter.eye_wink_right_index - 12] / 2
                model_input[ifm_converter.eye_wink_right_index - 12] = model_input[
                                                                           ifm_converter.eye_wink_right_index - 12] / 2

                uosum = model_input[ifm_converter.mouth_uuu_index - 12] + \
                        model_input[ifm_converter.mouth_ooo_index - 12]
                model_input[ifm_converter.mouth_ooo_index - 12] = uosum
                model_input[ifm_converter.mouth_uuu_index - 12] = 0
                is_open = (model_input[ifm_converter.mouth_aaa_index - 12] + model_input[
                    ifm_converter.mouth_iii_index - 12] + uosum) > 0
                model_input[ifm_converter.mouth_lowered_corner_left_index - 12] = 0
                model_input[ifm_converter.mouth_lowered_corner_right_index - 12] = 0
                model_input[ifm_converter.mouth_raised_corner_left_index - 12] = 0.5 if is_open else 0
                model_input[ifm_converter.mouth_raised_corner_right_index - 12] = 0.5 if is_open else 0
                simplify_arr[ifm_converter.mouth_lowered_corner_left_index] = 0
                simplify_arr[ifm_converter.mouth_lowered_corner_right_index] = 0
                simplify_arr[ifm_converter.mouth_raised_corner_left_index] = 0
                simplify_arr[ifm_converter.mouth_raised_corner_right_index] = 0
            if args.simplify >= 3:
                simplify_arr[ifm_converter.iris_rotation_x_index] = 20
                simplify_arr[ifm_converter.iris_rotation_y_index] = 20
                simplify_arr[ifm_converter.eye_wink_left_index] = 25
                simplify_arr[ifm_converter.eye_wink_right_index] = 25
                simplify_arr[ifm_converter.eye_happy_wink_left_index] = 25
                simplify_arr[ifm_converter.eye_happy_wink_right_index] = 25
            for i in range(12, len(simplify_arr)):
                if simplify_arr[i] > 0:
                    model_input[i - 12] = round(model_input[i - 12] * simplify_arr[i]) / simplify_arr[i]
            input_hash = hash(tuple(model_input))
            cached = model_cache.get(input_hash)
            tot += 1
            if not cached is None:
                self.output_queue.put_nowait(cached)
                model_cache.move_to_end(input_hash)
                hit += 1
                if (args.perf) or hit % 10 == 0:
                    print('cached', str(hit / tot * 100) + '%')
            else:
                if args.perf:
                    tic = time.perf_counter()
                for i in range(27):
                    mouth_eye_vector[0, i] = model_input[i]
                for i in range(3):
                    pose_vector[0, i] = model_input[i + 27]
                if model is None:
                    output_image = input_image
                else:
                    output_image = model(input_image, mouth_eye_vector, pose_vector)
                if args.perf:
                    torch.cuda.synchronize()
                    print("model", (time.perf_counter() - tic) * 1000)
                    tic = time.perf_counter()
                postprocessed_image = output_image.cpu()
                if args.perf:
                    print("cpu()", (time.perf_counter() - tic) * 1000)
                    tic = time.perf_counter()
                postprocessed_image = postprocessing_image(postprocessed_image)
                if args.perf:
                    print("postprocess", (time.perf_counter() - tic) * 1000)
                    tic = time.perf_counter()

                self.output_queue.put_nowait(postprocessed_image)
                if args.max_cache_len > 0:
                    model_cache[input_hash] = postprocessed_image
                    if len(model_cache) > args.max_cache_len:
                        model_cache.popitem(0)


@torch.no_grad()
def main():
    img = Image.open(f"character/{args.character}.png")
    wRatio = img.size[0] / 256
    img = img.resize((256, int(img.size[1] / wRatio)))
    input_image = preprocessing_image(img.crop((0, 0, 256, 256))).unsqueeze(0)
    extra_image = None
    if img.size[1] > 256:
        extra_image = np.array(img.crop((0, 256, img.size[0], img.size[1])))

    print("Character Image Loaded:", args.character)
    cap = None

    if not args.debug_input:

        if args.ifm is not None:
            client_process = IFMClientProcess()
            client_process.daemon = True
            client_process.start()
            print("IFM Service Running:", args.ifm)

        else:

            if args.input == 'cam':
                cap = cv2.VideoCapture(0)
                ret, frame = cap.read()
                if ret is None:
                    raise Exception("Can't find Camera")
            else:
                cap = cv2.VideoCapture(args.input)
                frame_count = 0
                os.makedirs(os.path.join('dst', args.character, args.output_dir), exist_ok=True)
                print("Webcam Input Running")

    facemesh = mp.solutions.face_mesh.FaceMesh(refine_landmarks=True)

    if args.output_webcam:
        cam_scale = 1
        cam_width_scale = 1
        if args.anime4k:
            cam_scale = 2
        if args.alpha_split:
            cam_width_scale = 2
        cam = pyvirtualcam.Camera(width=args.output_w * cam_scale * cam_width_scale, height=args.output_h * cam_scale,
                                  fps=60,
                                  backend=args.output_webcam,
                                  fmt=
                                  {'unitycapture': pyvirtualcam.PixelFormat.RGBA, 'obs': pyvirtualcam.PixelFormat.RGB}[
                                      args.output_webcam])
        print(f'Using virtual camera: {cam.device}')

    a = None

    if args.anime4k:
        parameters = ac.Parameters()
        # enable HDN for ACNet
        parameters.HDN = True

        # a = ac.AC(
        #     managerList=ac.ManagerList([ac.CUDAManager(dID=0)]),
        #     type=ac.ProcessorType.Cuda_ACNet,
        # )

        a = ac.AC(
            managerList=ac.ManagerList([ac.OpenCLACNetManager(pID=0, dID=0)]),
            type=ac.ProcessorType.OpenCL_ACNet,
        )
        a.set_arguments(parameters)
        print("Anime4K Loaded")

    position_vector = [0, 0, 0, 1]

    pose_queue = []
    blender_data = create_default_blender_data()
    tic1 = 0

    model_output = None
    model_process = ModelClientProcess(input_image)
    model_process.daemon = True
    model_process.start()

    print("Ready. Close this console to exit.")

    while True:
        # ret, frame = cap.read()
        # input_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        # results = facemesh.process(input_frame)

        if args.perf:
            tic = time.perf_counter()
        if args.debug_input:
            mouth_eye_vector_c = [0.0] * 27
            pose_vector_c = [0.0] * 3

            mouth_eye_vector_c[2] = math.sin(time.perf_counter() * 3)
            mouth_eye_vector_c[3] = math.sin(time.perf_counter() * 3)

            mouth_eye_vector_c[14] = 0

            mouth_eye_vector_c[25] = math.sin(time.perf_counter() * 2.2) * 0.2
            mouth_eye_vector_c[26] = math.sin(time.perf_counter() * 3.5) * 0.8

            pose_vector_c[0] = math.sin(time.perf_counter() * 1.1)
            pose_vector_c[1] = math.sin(time.perf_counter() * 1.2)
            pose_vector_c[2] = math.sin(time.perf_counter() * 1.5)



        elif args.ifm is not None:
            # get pose from ifm
            try:
                new_blender_data = blender_data
                while not client_process.should_terminate.value and not client_process.queue.empty():
                    new_blender_data = client_process.queue.get_nowait()
                blender_data = new_blender_data
            except queue.Empty:
                pass

            ifacialmocap_pose_converted = ifm_converter.convert(blender_data)

            # ifacialmocap_pose = blender_data
            #
            # eye_l_h_temp = ifacialmocap_pose[EYE_BLINK_LEFT]
            # eye_r_h_temp = ifacialmocap_pose[EYE_BLINK_RIGHT]
            # mouth_ratio = (ifacialmocap_pose[JAW_OPEN] - 0.10)*1.3
            # x_angle = -ifacialmocap_pose[HEAD_BONE_X] * 1.5 + 1.57
            # y_angle = -ifacialmocap_pose[HEAD_BONE_Y]
            # z_angle = ifacialmocap_pose[HEAD_BONE_Z] - 1.57
            #
            # eye_x_ratio = (ifacialmocap_pose[EYE_LOOK_IN_LEFT] -
            #                ifacialmocap_pose[EYE_LOOK_OUT_LEFT] -
            #                ifacialmocap_pose[EYE_LOOK_IN_RIGHT] +
            #                ifacialmocap_pose[EYE_LOOK_OUT_RIGHT]) / 2.0 / 0.75
            #
            # eye_y_ratio = (ifacialmocap_pose[EYE_LOOK_UP_LEFT]
            #                + ifacialmocap_pose[EYE_LOOK_UP_RIGHT]
            #                - ifacialmocap_pose[EYE_LOOK_DOWN_RIGHT]
            #                + ifacialmocap_pose[EYE_LOOK_DOWN_LEFT]) / 2.0 / 0.75

            mouth_eye_vector_c = [0.0] * 27
            pose_vector_c = [0.0] * 3
            for i in range(12, 39):
                mouth_eye_vector_c[i - 12] = ifacialmocap_pose_converted[i]
            for i in range(39, 42):
                pose_vector_c[i - 39] = ifacialmocap_pose_converted[i]

            position_vector = blender_data[HEAD_BONE_QUAT]


        else:
            ret, frame = cap.read()
            input_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            results = facemesh.process(input_frame)

            if results.multi_face_landmarks is None:
                continue

            facial_landmarks = results.multi_face_landmarks[0].landmark

            if args.debug:
                pose, debug_image = get_pose(facial_landmarks, frame)
            else:
                pose = get_pose(facial_landmarks)

            if len(pose_queue) < 3:
                pose_queue.append(pose)
                pose_queue.append(pose)
                pose_queue.append(pose)
            else:
                pose_queue.pop(0)
                pose_queue.append(pose)

            np_pose = np.average(np.array(pose_queue), axis=0, weights=[0.6, 0.3, 0.1])

            eye_l_h_temp = np_pose[0]
            eye_r_h_temp = np_pose[1]
            mouth_ratio = np_pose[2]
            eye_y_ratio = np_pose[3]
            eye_x_ratio = np_pose[4]
            x_angle = np_pose[5]
            y_angle = np_pose[6]
            z_angle = np_pose[7]

            mouth_eye_vector_c = [0.0] * 27
            pose_vector_c = [0.0] * 3

            mouth_eye_vector_c[2] = eye_l_h_temp
            mouth_eye_vector_c[3] = eye_r_h_temp

            mouth_eye_vector_c[14] = mouth_ratio * 1.5

            mouth_eye_vector_c[25] = eye_y_ratio
            mouth_eye_vector_c[26] = eye_x_ratio

            pose_vector_c[0] = (x_angle - 1.5) * 1.6
            pose_vector_c[1] = y_angle * 2.0  # temp weight
            pose_vector_c[2] = (z_angle + 1.5) * 2  # temp weight

        model_input_arr = mouth_eye_vector_c
        model_input_arr.extend(pose_vector_c)

        model_process.input_queue.put_nowait(model_input_arr)

        has_model_output = 0
        try:
            new_model_output = model_output
            while not model_process.output_queue.empty():
                has_model_output += 1
                new_model_output = model_process.output_queue.get_nowait()
            model_output = new_model_output
        except queue.Empty:
            pass
        if model_output is None:
            time.sleep(1)
            continue
        # print(has_model_output)
        # should_output=should_output or has_model_output
        # if not should_output:
        #     continue

        postprocessed_image = model_output

        if args.perf:
            print('===')
            print("input", time.perf_counter() - tic)
            tic = time.perf_counter()

        if extra_image is not None:
            postprocessed_image = cv2.vconcat([postprocessed_image, extra_image])

        k_scale = 1
        rotate_angle = 0
        dx = 0
        dy = 0
        if args.extend_movement is not None:
            k_scale = position_vector[2] * math.sqrt(args.extend_movement) + 1
            rotate_angle = -position_vector[0] * 40 * args.extend_movement
            dx = position_vector[0] * 400 * k_scale * args.extend_movement
            dy = -position_vector[1] * 600 * k_scale * args.extend_movement
        rm = cv2.getRotationMatrix2D((128, 128), rotate_angle, k_scale)
        rm[0, 2] += dx + args.output_w / 2 - 128
        rm[1, 2] += dy + args.output_h / 2 - 128

        postprocessed_image = cv2.warpAffine(
            postprocessed_image,
            rm,
            (args.output_w, args.output_h))

        if args.perf:
            print("extendmovement", (time.perf_counter() - tic) * 1000)
            tic = time.perf_counter()

        if args.anime4k:
            alpha_channel = postprocessed_image[:, :, 3]
            alpha_channel = cv2.resize(alpha_channel, None, fx=2, fy=2)

            # a.load_image_from_numpy(cv2.cvtColor(postprocessed_image, cv2.COLOR_RGBA2RGB), input_type=ac.AC_INPUT_RGB)
            # img = cv2.imread("character/test41.png")
            img1 = cv2.cvtColor(postprocessed_image, cv2.COLOR_RGBA2BGR)
            # a.load_image_from_numpy(img, input_type=ac.AC_INPUT_BGR)
            a.load_image_from_numpy(img1, input_type=ac.AC_INPUT_BGR)
            a.process()
            postprocessed_image = a.save_image_to_numpy()
            postprocessed_image = cv2.merge((postprocessed_image, alpha_channel))
            postprocessed_image = cv2.cvtColor(postprocessed_image, cv2.COLOR_BGRA2RGBA)
            if args.perf:
                print("anime4k", (time.perf_counter() - tic) * 1000)
                tic = time.perf_counter()
        if args.alpha_split:
            alpha_image = cv2.merge(
                [postprocessed_image[:, :, 3], postprocessed_image[:, :, 3], postprocessed_image[:, :, 3]])
            alpha_image = cv2.cvtColor(alpha_image, cv2.COLOR_RGB2RGBA)
            postprocessed_image = cv2.hconcat([postprocessed_image, alpha_image])

        if args.debug:
            output_frame = cv2.cvtColor(postprocessed_image, cv2.COLOR_RGBA2BGRA)
            # resized_frame = cv2.resize(output_frame, (np.min(debug_image.shape[:2]), np.min(debug_image.shape[:2])))
            # output_frame = np.concatenate([debug_image, resized_frame], axis=1)
            toc = time.perf_counter()
            fps = 1 / (toc - tic1)
            tic1 = toc
            cv2.putText(output_frame, str('%.1f' % fps), (0, 16), cv2.FONT_HERSHEY_PLAIN, 1, (0, 255, 0), 1)
            # cv2.putText(output_frame, str('%.1f' % (missed_count/changed_count*100)), (0, 48), cv2.FONT_HERSHEY_PLAIN, 1, (0, 255, 0), 1)
            cv2.imshow("frame", output_frame)
            # cv2.imshow("camera", debug_image)
            cv2.waitKey(1)
        if args.output_webcam:
            # result_image = np.zeros([720, 1280, 3], dtype=np.uint8)
            # result_image[720 - 512:, 1280 // 2 - 256:1280 // 2 + 256] = cv2.resize(
            #     cv2.cvtColor(postprocessing_image(output_image.cpu()), cv2.COLOR_RGBA2RGB), (512, 512))
            result_image = postprocessed_image
            if args.output_webcam == 'obs':
                result_image = cv2.cvtColor(result_image, cv2.COLOR_RGBA2RGB)
            cam.send(result_image)
            cam.sleep_until_next_frame()
        if args.perf:
            print("output", (time.perf_counter() - tic) * 1000)
            tic = time.perf_counter()


if __name__ == '__main__':
    main()
