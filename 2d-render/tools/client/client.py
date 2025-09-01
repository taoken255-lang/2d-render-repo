import grpc
from grpc import ssl_channel_credentials
from proto.render_service_pb2_grpc import RenderServiceStub
from proto.render_service_pb2 import RenderRequest, ImageChunk, AudioChunk
# import cv2
from loguru import logger
import wave
# import imageio
# import numpy as np
from PIL import Image
import time
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


def sending_audio(audio_file, image_file):
	with wave.open(audio_file, 'rb') as wf:
		logger.info("FILE OPENED")
		image_file_width = Image.open(image_file).width
		image_file_height = Image.open(image_file).height
		logger.info(image_file_width)
		logger.info(image_file_height)
		with open(image_file, "rb") as file:
			image_bytes = file.read()
			yield RenderRequest(image=ImageChunk(data=image_bytes, width=image_file_width, height=image_file_height), online=True, alpha=False, output_format="RGB")
		logger.info("IMG SENT")
		sample_rate = wf.getframerate()
		channels = wf.getnchannels()
		sample_width = wf.getsampwidth()
		bps = 16
		audio_chunk = 1 * sample_rate
		logger.info(audio_chunk)
		logger.info((sample_rate, 16))
		while chunk := wf.readframes(audio_chunk):
			# for _ in range(4):
			# 	chunk += chunk
			# for _ in range(30):
			yield RenderRequest(audio=AudioChunk(data=chunk, sample_rate=sample_rate, bps=bps))
			logger.info("AUD SENT")
			time.sleep(0.3)


def run(audio_file, image_file):
	creds = ssl_channel_credentials()
	channel = grpc.secure_channel("2d.digitalavatars.ru", credentials=creds, options=[
		('grpc.max_receive_message_length', 10 * 1024 * 1024),
		('grpc.max_send_message_length', 10 * 1024 * 1024),
	])
	stub = RenderServiceStub(channel)

	response_stream = stub.RenderStream(sending_audio(audio_file, image_file))

	for idxa, response_chunk in enumerate(response_stream):
		logger.info("SAVE IMAGE")
		logger.info(idxa)
		logger.info((response_chunk.height, response_chunk.width))
		img = Image.frombytes("RGB", (response_chunk.width, response_chunk.height), response_chunk.data, "raw")
		img.save(f"tools/client/imgs/frame_{idxa}.png")


def local_run(audio_file, image_file):
	channel = grpc.insecure_channel("localhost:8500", options=[
		('grpc.max_receive_message_length', 10 * 1024 * 1024),
		('grpc.max_send_message_length', 10 * 1024 * 1024),
	])
	stub = RenderServiceStub(channel)
	response_stream = stub.RenderStream(sending_audio(audio_file, image_file))
	full_times = []
	first_counter = time.perf_counter()
	for idxa, response_chunk in enumerate(response_stream):
		# tstart = time()
		rec_counter = time.perf_counter()
		full_times.append(rec_counter - first_counter)
		logger.info("SAVE IMAGE")
		first_counter = rec_counter
		logger.info(idxa)
		logger.info((response_chunk.width, response_chunk.height))
		# img = Image.frombytes("RGBA", (response_chunk.height, response_chunk.width), response_chunk.data, "raw", "BGRA")
		# img = segment_person_from_pil_image(img)
		# cv2.imwrite(f"imgs/frame_{idxa}.png", img)
		# logger.info(f"{(time() - tstart) * 1000} ms")

		# img.save(f"imgs/frame_{idxa}.png")
	logger.info(full_times)


if __name__ == '__main__':
	audio_file = "tools/client/res/test2.wav"
	image_file = "tools/client/res/img2.png"
	run(audio_file, image_file)  # python -m tools.client.client
	# local_run(audio_file, image_file)
