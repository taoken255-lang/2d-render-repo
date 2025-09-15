import grpc
from grpc import ssl_channel_credentials
from proto.render_service_pb2_grpc import RenderServiceStub
from proto.render_service_pb2 import RenderRequest, ImageChunk, AudioChunk, PlayAnimation, SetAvatar, SetEmotion, InfoRequest
# import cv2
from loguru import logger
import wave
# import imageio
# import numpy as np
from PIL import Image
import time
from queue import Queue


# from time import time

# def sending_audio():
# 	logger.info("FILE OPENED")
# 	image_file_width = Image.open("res/img5.png").width
# 	image_file_height = Image.open("res/img5.png").height
# 	logger.info(image_file_width)
# 	logger.info(image_file_height)
# 	with open("res/img5.png", "rb") as file:
# 		image_bytes = file.read()
# 		yield RenderRequest(image=ImageChunk(data=image_bytes, width=image_file_width, height=image_file_height),
# 		                    online=True, alpha=False)
# 	logger.info("IMG SENT")
# 	for _ in range(5):
# 		with wave.open('res/aioffice.wav', 'rb') as wf:
# 			sample_rate = wf.getframerate()
# 			channels = wf.getnchannels()
# 			sample_width = wf.getsampwidth()
# 			bps = 16
# 			audio_chunk = 1 * sample_rate
# 			logger.info(audio_chunk)
# 			logger.info((sample_rate, 16))
# 			while chunk := wf.readframes(audio_chunk):
# 				# for _ in range(4):
# 				# 	chunk += chunk
# 				# for _ in range(30):
# 				yield RenderRequest(audio=AudioChunk(data=chunk, sample_rate=sample_rate, bps=bps), online=False)
# 				logger.info("AUD SENT")
# 				time.sleep(0.3)


def sending_audio(audio_file, image_file, event_queue):
	with wave.open(audio_file, 'rb') as wf:
		logger.info("FILE OPENED")
		image_file_width = 1920
		image_file_height = 1080
		logger.info(image_file_width)
		logger.info(image_file_height)
		with open(image_file, "rb") as file:
			image_bytes = file.read()
			yield RenderRequest(image=ImageChunk(data=image_bytes, width=image_file_width, height=image_file_height),
			                    online=True, alpha=False, output_format="RGB")
			yield RenderRequest(set_avatar=SetAvatar(avatar_id="blue_woman"))
		logger.info("IMG SENT")

		sample_rate = wf.getframerate()
		channels = wf.getnchannels()
		sample_width = wf.getsampwidth()
		bps = 16
		audio_chunk = 1 * sample_rate
		logger.info(audio_chunk)
		logger.info((sample_rate, 16))
		anim_idx = 2
		cur_idx = 0
		while chunk := wf.readframes(audio_chunk):
			# for _ in range(4):
			# 	chunk += chunk
			# for _ in range(30):
			# _ = event_queue.get()
			yield RenderRequest(audio=AudioChunk(data=chunk, sample_rate=sample_rate, bps=bps))
			logger.info("AUD SENT")

			if cur_idx == anim_idx:  # or cur_idx == anim_idx + 1:
				yield RenderRequest(play_animation=PlayAnimation(animation="idle"))
				yield RenderRequest(play_animation=PlayAnimation(animation="idle"))
				yield RenderRequest(play_animation=PlayAnimation(animation="idle"))
				yield RenderRequest(play_animation=PlayAnimation(animation="idle"))
				yield RenderRequest(play_animation=PlayAnimation(animation="idle"))
				# yield RenderRequest(play_animation=PlayAnimation(animation="point_suit"))
				# yield RenderRequest(set_emotion=SetEmotion(emotion="angry"))
				# logger.info("EMOTION SENT")
				# yield RenderRequest(play_animation=PlayAnimation(animation="point_suit"))
			# 	# yield RenderRequest(play_animation=PlayAnimation(animation="talk_suit"))
			# 	# yield RenderRequest(play_animation=PlayAnimation(animation="idle"))
			# 	logger.info("sent play animation command")
			# elif cur_idx == anim_idx + 2:
			# 	yield RenderRequest(set_emotion=SetEmotion(emotion="happy"))
			# 	logger.info("EMOTION SENT")
			# elif cur_idx == anim_idx + 4:
			# 	yield RenderRequest(set_emotion=SetEmotion(emotion="sad"))
			# 	logger.info("EMOTION SENT")
			# elif cur_idx == anim_idx + 6:
			# 	yield RenderRequest(set_emotion=SetEmotion(emotion="angry"))
			# 	logger.info("EMOTION SENT")

			time.sleep(0.3)
			cur_idx += 1


def run(audio_file, image_file, event_queue):
	creds = ssl_channel_credentials()
	channel = grpc.secure_channel("2d-dev.digitalavatars.ru", credentials=creds, options=[
		('grpc.max_receive_message_length', 10 * 1024 * 1024),
		('grpc.max_send_message_length', 10 * 1024 * 1024),
	])
	stub = RenderServiceStub(channel)

	response_stream = stub.RenderStream(sending_audio(audio_file, image_file, event_queue))
	img_idx = 0
	for idxa, response_chunk in enumerate(response_stream):
		logger.info(f"CHUNK {idxa}")
		if response_chunk.WhichOneof("chunk") == "video":
			logger.info(f"SAVE IMAGE {img_idx}")
			logger.info(img_idx)
			# logger.info((response_chunk.height, response_chunk.width))
			img = Image.frombytes("RGB", (response_chunk.video.width, response_chunk.video.height),
			                      response_chunk.video.data, "raw")
			img.save(f"tools/client/imgs/frame_{img_idx}.png")
			img_idx += 1
		elif response_chunk.WhichOneof("chunk") == "start_animation":
			logger.info(f"START ANIMATION {response_chunk.start_animation.animation_name}")
		elif response_chunk.WhichOneof("chunk") == "end_animation":
			logger.info(f"END ANIMATION {response_chunk.end_animation.animation_name}")
		elif response_chunk.WhichOneof("chunk") == "emotion_set":
			logger.info(f"EMOTION SET {response_chunk.emotion_set.emotion_name}")
		# if img_idx % 10 == 0:
		# 	event_queue.put(1)


def local_run(audio_file, image_file, event_queue):
	channel = grpc.insecure_channel("localhost:8502", options=[
		('grpc.max_receive_message_length', 10 * 1024 * 1024),
		('grpc.max_send_message_length', 10 * 1024 * 1024),
	])
	stub = RenderServiceStub(channel)
	response_stream = stub.RenderStream(sending_audio(audio_file, image_file, event_queue))
	full_times = []
	# first_counter = time.perf_counter()
	img_idx = 0
	for idxa, response_chunk in enumerate(response_stream):
		# # tstart = time()
		# rec_counter = time.perf_counter()
		# full_times.append(rec_counter - first_counter)
		# logger.info("SAVE IMAGE")
		# first_counter = rec_counter
		# logger.info(idxa)
		# logger.info((response_chunk.width, response_chunk.height))
		# # img = Image.frombytes("RGBA", (response_chunk.height, response_chunk.width), response_chunk.data, "raw", "BGRA")
		# # img = segment_person_from_pil_image(img)
		# # cv2.imwrite(f"imgs/frame_{idxa}.png", img)
		# # logger.info(f"{(time() - tstart) * 1000} ms")

		# img.save(f"imgs/frame_{idxa}.png")

		logger.info(f"CHUNK {idxa}")
		if response_chunk.WhichOneof("chunk") == "video":
			logger.info(f"SAVE IMAGE {img_idx}")
			logger.info(img_idx)
			# logger.info((response_chunk.height, response_chunk.width))
			img = Image.frombytes("RGB", (response_chunk.video.width, response_chunk.video.height),
			                      response_chunk.video.data, "raw")
			img.save(f"tools/client/imgs/frame_{img_idx}.png")
			img_idx += 1
		elif response_chunk.WhichOneof("chunk") == "start_animation":
			logger.info(f"START ANIMATION {response_chunk.start_animation.animation_name}")
		elif response_chunk.WhichOneof("chunk") == "end_animation":
			logger.info(f"END ANIMATION {response_chunk.end_animation.animation_name}")
		elif response_chunk.WhichOneof("chunk") == "emotion_set":
			logger.info(f"EMOTION SET {response_chunk.emotion_set.emotion_name}")
		# if img_idx % 10 == 0:
			# event_queue.put(1)
	logger.info(full_times)


def info():
	creds = ssl_channel_credentials()
	channel = grpc.secure_channel("2d-dev.digitalavatars.ru", credentials=creds, options=[
		('grpc.max_receive_message_length', 10 * 1024 * 1024),
		('grpc.max_send_message_length', 10 * 1024 * 1024),
	])
	stub = RenderServiceStub(channel)
	info_response = stub.InfoRouter(InfoRequest())
	avatars = info_response.animations
	for avatar in avatars:
		logger.info(f"{str(avatar)}: {info_response.animations[avatar].items}")
		logger.info(f"{str(avatar)}: {info_response.emotions[avatar].items}")


if __name__ == '__main__':
	event_queue = Queue()
	event_queue.put(1)
	event_queue.put(1)
	audio_file = "tools/client/res/Ermakova.wav"
	image_file = "tools/client/res/img3.png"
	# info()
	run(audio_file, image_file, event_queue)  # python -m tools.client.client
	# local_run(audio_file, image_file, event_queue)
