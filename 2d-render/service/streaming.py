from proto import render_service_pb2, render_service_pb2_grpc
from service.render import RenderService
from loguru import logger
import threading
from threading import Event
from multiprocessing import Queue as mQueue, Process, Value
import numpy as np
import time
from queue import Queue as tQueue
from grpc import RpcError
from uuid import uuid4
from enum import Enum


#
# gc.disable()


class IPCDataType(Enum):
	IMAGE = 1
	AUDIO = 2
	COMMAND = 3


class CommandDataType(Enum):
	SetAvatar = 1
	PlayAnimation = 2
	SetEmotion = 3


class IPCObject:
	def __init__(self, data_type: IPCDataType, data: object):
		self.data_type = data_type
		self.data = data


class ImageObject:
	def __init__(self, data: bytes, height: int, width: int):
		self.data = data
		self.height = height
		self.width = width


class AudioObject:
	def __init__(self, data: bytes, sample_rate: int, bps: int):
		self.data = data
		self.sample_rate = sample_rate
		self.bps = bps


class CommandObject:
	def __init__(self, command_type: CommandDataType, command_data: str):
		self.command_type = command_type
		self.command_data = command_data


class SharedString:
	def __init__(self, value):
		self._value = value
		self._lock = threading.Lock()

	def set(self, value):
		with self._lock:
			self._value = value

	def get(self):
		with self._lock:
			return self._value


#
# class InputObject:
#     def __init__(self, image: ImageObject = None, audio: AudioObject = None):
#         self.image = image
#         self.audio = audio


def render_stream(chunk, render, is_last=False):
	BitsPerSample = 16
	NumChannels = 1
	SampleRate = 16000
	ChunkDuration = 2.0
	logger.debug("RENDER GOT AUDIO")

	if is_last:
		logger.debug("RENDER GOT LAST AUDIO - None")
		render.render_chunk(None, frame_rate=0, is_last=True)
		return

	aud_chunk = chunk.data

	aud_sample_rate = aud_chunk.sample_rate if aud_chunk.sample_rate != 0 and aud_chunk.sample_rate else SampleRate
	aud_bps = aud_chunk.bps if aud_chunk.bps != 0 and aud_chunk.bps else BitsPerSample
	aud_data = aud_chunk.data

	logger.debug(f"AUDIO DATA {len(aud_data)}, {aud_bps}, {aud_sample_rate}")
	# aud_time = len(aud_data) / ((aud_bps / 8) * aud_sample_rate)
	render.render_chunk(aud_data, frame_rate=aud_sample_rate, is_last=is_last)


def stream_frames_thread(render, video_queue, height, width, start_time, request_id):
	with logger.contextualize(request_id=request_id):
		# time_chunks_list = []
		# dt_full_time = 0
		# dt_min_time = 1000
		# dt_max_time = 0
		# dt_chunk_counter = 0
		# dt_first_chunk_flag = False
		# dt_first_cur_time = 0
		# good_chunks = 0
		# bad_chunks = 0
		# gb_info = ""
		for frame in render.sdk.stream_frames():
			# if not dt_first_chunk_flag:  # первый чанк
			# 	dt_first_cur_time = time.perf_counter()

			# dt_cur_time = time.perf_counter()
			# if dt_first_chunk_flag:  # все кроме первого чанка
			# 	dt_chunk_time = dt_cur_time - dt_start_time
			# 	if dt_chunk_time >= dt_max_time:
			# 		dt_max_time = dt_chunk_time
			# 	if dt_chunk_time <= dt_min_time:
			# 		dt_min_time = dt_chunk_time
			# 	dt_full_time += dt_chunk_time
			# 	dt_chunk_counter += 1
			# 	logger.info(f"DITTO CHUNK TIME: {dt_chunk_time}")
			# start_time.value = render.start_time
			video_queue.put(ImageObject(data=frame, height=height, width=width))
			# dt_start_time = dt_cur_time
			# dt_first_chunk_flag = True
			# time_difference = dt_cur_time - dt_first_cur_time
			# req_time_difference = 0.04 * dt_chunk_counter
			# logger.info(f"TIME DIFFERENCES (req:act) {req_time_difference}:{time_difference}")
			# time_chunks_list.append(time_difference)
			# if time_difference <= req_time_difference:
			# 	good_chunks += 1
			# 	gb_info += "1"
			# else:
			# 	bad_chunks += 1
			# 	gb_info += "0"
		video_queue.put(ImageObject(data=None, height=height, width=width))
		# logger.info(f"MIN DITTO OUTPUT CHUNK TIME: {dt_min_time}")
		# logger.info(f"MAX DITTO OUTPUT CHUNK TIME: {dt_max_time}")
		# logger.info(f"AVG DITTO OUTPUT CHUNK TIME: {dt_full_time / dt_chunk_counter}")
		# logger.info(f"FULL DITTO OUTPUT TIME: {dt_full_time}")
		# logger.info(f"CHUNKS REALTIME INFO (g:b): {good_chunks}:{bad_chunks}")
		# logger.info(f"CHUNKS REALTIME INFO (g:b): {gb_info}")
		# logger.info(time_chunks_list)


def start_render_process(audio_queue, video_queue, start_time, request_id):
	with logger.contextualize(request_id=request_id):
		# gc.disable()
		is_online_chunk = True
		while True:
			chunk = audio_queue.get()
			if is_online_chunk:
				render = RenderService(is_online=chunk["is_online"])
				is_online_chunk = False
				continue

			if chunk is None:
				render_stream(chunk=chunk, render=render, is_last=True)
				break

			if chunk.data_type == IPCDataType.IMAGE:
				logger.info("RECEIVE IMAGE CHUNK")
				img_chunk = chunk.data

				img_height = img_chunk.height
				img_width = img_chunk.width
				img_data = img_chunk.data
				render.handle_image(image_chunk=img_data)
				logger.debug("START STREAMING THREAD")
				stream_thread = threading.Thread(target=stream_frames_thread, args=(
					render, video_queue, img_height, img_width, start_time, request_id,))
				stream_thread.start()

			elif chunk.data_type == IPCDataType.AUDIO:
				logger.info("RECEIVE AUDIO CHUNK")
				render_stream(chunk=chunk, render=render)

			elif chunk.data_type == IPCDataType.COMMAND:
				if chunk.data.command_type == CommandDataType.SetAvatar:
					render.set_avatar(avatar_id=chunk.data.command_data)

				elif chunk.data.command_type == CommandDataType.PlayAnimation:
					render.play_animation(animation=chunk.data.command_data)

				elif chunk.data.command_type == CommandDataType.SetEmotion:
					render.set_emotion(emotion=chunk.data.command_data)

		logger.info("START CLOSING PROCESSES IN PROCESS")
		render.sdk.close()
		logger.info("FINISH JOINING THREAD IN PROCESS")
		stream_thread.join()
		logger.info("FINISH CLOSING PROCESSES IN PROCESS")


def reader_thread(request_iterator, audio_queue, is_online, is_alpha, output_format, request_id, context):
	with logger.contextualize(request_id=request_id):
		try:
			for chunk in request_iterator:
				if chunk.image.data:
					logger.info(f"ONLINE MODE: {chunk.online}")
					logger.info(f"ALPHA MODE: {chunk.alpha}")
					logger.info(f"IMAGE SIZE: {chunk.image.width}x{chunk.image.height}")
					if chunk.online:
						is_online.set()
					if chunk.alpha:
						is_alpha.set()
					output_format.set("BGRA" if chunk.output_format == "" else chunk.output_format)
					audio_queue.put({"is_online": chunk.online})
					audio_queue.put(IPCObject(data_type=IPCDataType.IMAGE,
					                          data=ImageObject(data=chunk.image.data,
					                                           height=chunk.image.height,
					                                           width=chunk.image.width)))

				elif chunk.audio.data:
					bps_bytes = int(chunk.audio.bps / 8)
					chunk_sum = len(chunk.audio.data) / (bps_bytes * chunk.audio.sample_rate)  # фактическое время
					req_sum = int(bps_bytes * chunk.audio.sample_rate * 1)  # количество байт для 1 секунды
					if chunk_sum > 1:  # 1 секунда
						it_num = int(-(-chunk_sum // 1))
						for it in range(it_num):
							l_border = it * req_sum
							r_border = (it + 1) * req_sum
							cut_data = chunk.audio.data[l_border: r_border]
							audio_queue.put(IPCObject(data_type=IPCDataType.AUDIO,
							                          data=AudioObject(data=cut_data,
							                                           sample_rate=chunk.audio.sample_rate,
							                                           bps=chunk.audio.bps)))

					else:
						audio_queue.put(IPCObject(data_type=IPCDataType.AUDIO,
						                          data=AudioObject(data=chunk.audio.data,
						                                           sample_rate=chunk.audio.sample_rate,
						                                           bps=chunk.audio.bps)))

				elif chunk.WhichOneof("command") == "set_avatar":
					audio_queue.put(IPCObject(data_type=IPCDataType.COMMAND,
					                          data=CommandObject(command_type=CommandDataType.SetAvatar,
					                                             command_data=chunk.set_avatar.avatar_id)))

				elif chunk.WhichOneof("command") == "play_animation":
					audio_queue.put(IPCObject(data_type=IPCDataType.COMMAND,
					                          data=CommandObject(command_type=CommandDataType.PlayAnimation,
					                                             command_data=chunk.play_animation.animation)))

				elif chunk.WhichOneof("command") == "set_emotion":
					audio_queue.put(IPCObject(data_type=IPCDataType.COMMAND,
					                          data=CommandObject(command_type=CommandDataType.SetEmotion,
					                                             command_data=chunk.set_emotion.emotion)))

		except RpcError as e:
			logger.error(f"GOT RPCERROR EXCEPTION IN READER THREAD {str(e)} START CLOSING PROCESS")
			context.cancel()
		except Exception as e:
			logger.error(f"GOT EXCEPTION IN READER THREAD {str(e)} START CLOSING PROCESS")
		except BaseException as e:
			logger.error(f"GOT BASEEXCEPTION IN READER THREAD {str(e)} START CLOSING PROCESS")
		audio_queue.put(None)
		logger.info("END REQUEST ITERATOR")


def get_mqueue_thread(from_queue, to_queue, is_alpha, output_format, alpha_service, request_id):
	with logger.contextualize(request_id=request_id):
		try:
			while True:
				frame = from_queue.get()
				logger.debug("CHUNK DITTO -> LAST")
				if frame.data is None:
					logger.debug("CHUNK DITTO IS NONE - BREAK")
					break

				rgb = np.frombuffer(frame.data.tobytes(), dtype=np.uint8).reshape(frame.width, frame.height, 3)
				if is_alpha.is_set():
					bgra = (rgb[..., [0, 1, 2]]).tobytes()
					frame.data = bgra
					segm_start = time.perf_counter()
					new_data = alpha_service.segment_person_from_pil_image(frame_in=frame)
					segm_end = time.perf_counter()
					logger.debug(f"SEGMENTATION TIME: {segm_end - segm_start}")
					frame = ImageObject(
						data=new_data,
						width=frame.width,
						height=frame.height
					)

				else:
					if output_format.get() == "BGRA":
						logger.debug("START NUMPY CONVERTATIONS")
						bgra = np.empty((frame.width, frame.height, 4), dtype=np.uint8)

						bgra[..., 0] = rgb[..., 2]  # B
						bgra[..., 1] = rgb[..., 1]  # G
						bgra[..., 2] = rgb[..., 0]  # R
						bgra[..., 3] = 255  # A

						bgra_bytes = bgra.tobytes()
						logger.debug("END NUMPY CONVERTATIONS")
						frame = ImageObject(
							data=bgra_bytes,
							width=frame.width,
							height=frame.height
						)
					else:
						rgb_bytes = rgb.tobytes()
						frame = ImageObject(
							data=rgb_bytes,
							width=frame.width,
							height=frame.height
						)

				# frame = ImageObject(
				#     data="CHUNK".encode('utf-8'),
				#     width=frame.width,
				#     height=frame.height
				# )
				to_queue.put(frame)
		except RpcError as e:
			logger.error(f"GOT RPCERROR EXCEPTION IN GET MQUEUE THREAD {str(e)} START CLOSING PROCESS")
		except Exception as e:
			logger.error(f"GOT EXCEPTION IN GET MQUEUE THREAD {str(e)} START CLOSING PROCESS")
		except BaseException as e:
			logger.error(f"GOT BASEEXCEPTION IN GET MQUEUE THREAD {str(e)} START CLOSING PROCESS")
		to_queue.put(ImageObject(data=None, width=None, height=None))


class StreamingService(render_service_pb2_grpc.RenderServiceServicer):
	def __init__(self, alpha_service):
		self.alpha_service = alpha_service

	def RenderStream(self, request_iterator, context):
		request_id = str(uuid4())
		with logger.contextualize(request_id=request_id):
			audio_queue = mQueue()
			video_queue = mQueue()
			local_queue = tQueue()
			is_online = Event()
			is_alpha = Event()
			output_format = SharedString("")
			full_start_time = Value('d', 0.0)
			# full_times_list = []
			render_process = Process(target=start_render_process,
			                         args=(audio_queue, video_queue, full_start_time, request_id,))
			render_process.start()

			reader_trd = threading.Thread(target=reader_thread, args=(
				request_iterator, audio_queue, is_online, is_alpha, output_format, request_id, context))
			reader_trd.start()

			mqueue_trd = threading.Thread(target=get_mqueue_thread,
			                              args=(video_queue, local_queue, is_alpha, output_format, self.alpha_service, request_id,))
			mqueue_trd.start()

			first_chunk_flag = False
			# first_img_flag = True
			# full_time = 0
			# min_time = 1000
			# max_time = 0
			# chunk_counter = 0
			try:
				while True:
					frame = local_queue.get()
					logger.debug("CHUNK LAST -> OUTPUT")
					if frame.data is None:
						logger.debug("CHUNK LAST IS NONE - BREAK")
						break
					# cur_time = time.perf_counter()
					# if first_img_flag:
					#     img = Image.frombytes("RGBA", (frame.height, frame.width),
					#                           frame.data, "raw", "BGRA")
					#     img.save(f"./checkpoints/frame.png")
					#     first_img_flag = False
					# if first_chunk_flag:
					# 	cur_time = time.perf_counter()
					# 	chunk_time = cur_time - start_time
					# 	if chunk_time >= max_time:
					# 		max_time = chunk_time
					# 	if chunk_time <= min_time:
					# 		min_time = chunk_time
					# 	full_time += chunk_time
					# 	chunk_counter += 1
					# logger.info(f"GRPC CHUNK TIME: {chunk_time}")
					# logger.info(f"START GRPC YIELDING {chunk_counter}")
					# full_delta_time = time.perf_counter() - full_start_time.value
					# full_times_list.append(full_delta_time)
					active_client = context.is_active()
					logger.debug(f"CLIENT: {active_client}")
					if not active_client:
						raise RpcError
					logger.debug(f"SEND")
					try:
						yield render_service_pb2.VideoChunk(data=frame.data, width=frame.width, height=frame.height)
					except RpcError as e:
						break
					logger.info(f"SENT")
					# logger.info(f"END GRPC YIELDING {chunk_counter}")
					# start_time = cur_time
					# first_chunk_flag = True
			except RpcError as e:
				logger.error(f"GOT RPCERROR EXCEPTION IN RENDER STREAM {str(e)} START CLOSING PROCESS")
			except Exception as e:
				logger.error(f"GOT EXCEPTION IN RENDER STREAM {str(e)} START CLOSING PROCESS")
			except BaseException as e:
				logger.error(f"GOT BASEEXCEPTION IN RENDER STREAM {str(e)} START CLOSING PROCESS")
			video_queue.put(ImageObject(data=None, height=None, width=None))
			logger.info(f"START JOINING PROCESS")
			render_process.terminate()
			logger.info(f"PROCESS FINISHED")
			reader_trd.join()
			logger.info(f"READER FINISHED")
			mqueue_trd.join()
			logger.info(f"MQUEUE FINISHED")
			# logger.info(f"MIN CHUNK TIME: {min_time}")
			# logger.info(f"MAX CHUNK TIME: {max_time}")
			# if chunk_counter != 0:
			# 	logger.info(f"AVG CHUNK TIME: {full_time / chunk_counter}")
			# logger.info(f"FULL TIME: {full_time}")
			# logger.info(f"ALL PROCESSES CLOSED")
			# logger.info(full_times_list)
			logger.info(f"COMPLETE")
