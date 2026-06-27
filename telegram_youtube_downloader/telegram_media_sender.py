import os
import uuid
import logging
import pathlib
import subprocess

import requests

from telegram_youtube_downloader.utils.file_utils import FileUtils
from telegram_youtube_downloader.errors.send_error import SendError
from telegram_youtube_downloader.utils.config_utils import ConfigUtils
from telegram_youtube_downloader.utils.api_key_utils import ApiKeyUtils


class MultipartFileStream:
	def __init__(
		self,
		prefix: bytes,
		suffix: bytes,
		file_path: str,
		file_offset: int,
		file_size: int,
		chunk_size: int,
	) -> None:
		self.__prefix = prefix
		self.__suffix = suffix
		self.__file_path = file_path
		self.__file_offset = file_offset
		self.__file_size = file_size
		self.__chunk_size = chunk_size
		self.__content_length = len(prefix) + file_size + len(suffix)

		self.__prefix_offset = 0
		self.__suffix_offset = 0
		self.__remaining_file_size = file_size
		self.__file = open(file_path, "rb")
		self.__file.seek(file_offset)

	def __len__(self) -> int:
		return self.__content_length

	def close(self) -> None:
		self.__file.close()

	def read(self, size: int = -1) -> bytes:
		if size == 0:
			return b""

		remaining_read_size = self.__chunk_size if size < 0 else size
		chunks = []

		while remaining_read_size > 0:
			if self.__prefix_offset < len(self.__prefix):
				chunk = self.__prefix[
					self.__prefix_offset : self.__prefix_offset + remaining_read_size
				]
				self.__prefix_offset += len(chunk)

			elif self.__remaining_file_size > 0:
				chunk = self.__file.read(
					min(
						self.__chunk_size,
						remaining_read_size,
						self.__remaining_file_size,
					)
				)
				self.__remaining_file_size -= len(chunk)

			elif self.__suffix_offset < len(self.__suffix):
				chunk = self.__suffix[
					self.__suffix_offset : self.__suffix_offset + remaining_read_size
				]
				self.__suffix_offset += len(chunk)

			else:
				self.close()
				break

			if not chunk:
				break

			chunks.append(chunk)
			remaining_read_size -= len(chunk)

		return b"".join(chunks)


class TelegramMediaSender:
	"""Custom media sender class for telegrams native api"""

	__default_telegram_api_url = "https://api.telegram.org/bot"
	__max_telegram_upload_size_bytes = 230 * 1024 * 1024
	__file_split_chunk_size_bytes = 8 * 1024 * 1024
	__multipart_content_type = "application/octet-stream"
	__media_segment_target_size_bytes = 200 * 1024 * 1024

	def __init__(self) -> None:
		self.__telegram_options = ConfigUtils.get_app_config().telegram_bot_options
		self.__bot_key = ApiKeyUtils.get_telegram_bot_key()
		self.__logger = logging.getLogger(f"tyd.{self.__class__.__name__}")
		__base_url_config = ConfigUtils.get_app_config().telegram_bot_options.base_url
		self.__base_url = (
			__base_url_config if __base_url_config is not None else self.__default_telegram_api_url
		)

	def __delete_file_folder(self, file_path: str) -> None:
		folder_name, _ = os.path.split(file_path)
		FileUtils.delete_directory(folder_name, self.__logger)

	def __delete_directory(self, directory_path: str) -> None:
		FileUtils.delete_directory(directory_path, self.__logger)

	def __get_split_file_name(self, file_name: str, part_number: int, part_count: int) -> str:
		file_path = pathlib.Path(file_name)
		part_suffix = f".part{part_number:03d}-of-{part_count:03d}"

		return f"{file_path.stem}{part_suffix}{file_path.suffix}"

	def __get_media_duration_seconds(self, file_path: str) -> float:
		try:
			result = subprocess.run(
				[
					"ffprobe",
					"-v",
					"error",
					"-show_entries",
					"format=duration",
					"-of",
					"default=noprint_wrappers=1:nokey=1",
					file_path,
				],
				capture_output=True,
				check=True,
				text=True,
			)
			duration = float(result.stdout.strip())
		except Exception:
			self.__logger.error(f"Could not get media duration: {file_path}", exc_info=True)
			raise SendError("Could not split media file")

		if duration <= 0:
			self.__logger.error(f"Invalid media duration '{duration}' for file: {file_path}")
			raise SendError("Could not split media file")

		return duration

	def __get_segment_time_seconds(self, file_path: str, target_size_bytes: int) -> int:
		file_size = os.path.getsize(file_path)
		duration = self.__get_media_duration_seconds(file_path)
		bytes_per_second = file_size / duration

		return max(1, int(target_size_bytes / bytes_per_second))

	def __build_segment_file_name_template(self, file_path: str, file_name: str) -> str:
		download_dir = os.path.dirname(file_path)
		file_name_path = pathlib.Path(file_name)
		segment_dir = os.path.join(download_dir, "segments")
		pathlib.Path(segment_dir).mkdir(parents=True, exist_ok=True)

		return os.path.join(segment_dir, f"{file_name_path.stem}.part%03d{file_name_path.suffix}")

	def __split_media_file(self, file_path: str, file_name: str) -> list[tuple[str, str]]:
		segment_file_name_template = self.__build_segment_file_name_template(file_path, file_name)
		segment_dir = os.path.dirname(segment_file_name_template)
		segment_time = self.__get_segment_time_seconds(
			file_path,
			self.__media_segment_target_size_bytes,
		)

		self.__delete_directory(segment_dir)
		pathlib.Path(segment_dir).mkdir(parents=True, exist_ok=True)

		self.__logger.info(f"Splitting media file '{file_path}' with segment_time={segment_time}")
		result = subprocess.run(
			[
				"ffmpeg",
				"-y",
				"-i",
				file_path,
				"-map",
				"0",
				"-c",
				"copy",
				"-f",
				"segment",
				"-segment_time",
				str(segment_time),
				"-reset_timestamps",
				"1",
				segment_file_name_template,
			],
			capture_output=True,
			text=True,
		)

		if result.returncode != 0:
			self.__logger.error(
				f"Could not split media file, ffmpeg exit code {result.returncode}, stderr={result.stderr[-1000:]}"
			)
			raise SendError("Could not split media file")

		segment_paths = sorted(
			os.path.join(segment_dir, file_name)
			for file_name in os.listdir(segment_dir)
			if os.path.isfile(os.path.join(segment_dir, file_name))
		)

		if not segment_paths:
			self.__logger.error(f"Media split produced no segments: {file_path}")
			raise SendError("Could not split media file")

		largest_segment_size = max(os.path.getsize(segment_path) for segment_path in segment_paths)
		if largest_segment_size > self.__max_telegram_upload_size_bytes:
			self.__logger.error(
				f"Largest segment is too large ({largest_segment_size} bytes), "
				f"max upload size is {self.__max_telegram_upload_size_bytes} bytes"
			)
			raise SendError("Could not split media file below Telegram upload limit")

		part_count = len(segment_paths)
		return [
			(
				segment_path,
				self.__get_split_file_name(file_name, index, part_count),
			)
			for index, segment_path in enumerate(segment_paths, start=1)
		]

	def __get_multipart_field(self, boundary: str, name: str, value: str) -> bytes:
		return (
			f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
		).encode()

	def __get_multipart_file_header(self, boundary: str, field_name: str, file_name: str) -> bytes:
		return (
			f"--{boundary}\r\n"
			f'Content-Disposition: form-data; name="{field_name}"; filename="{file_name}"\r\n'
			f"Content-Type: {self.__multipart_content_type}\r\n\r\n"
		).encode()

	def __post_multipart_file(
		self,
		url: str,
		payload: dict[str, str | int],
		file_field_name: str,
		file_path: str,
		file_name: str,
		timeout: int,
		file_offset: int = 0,
		file_size: "int | None" = None,
	) -> dict:
		boundary = uuid.uuid4().hex
		file_size = os.path.getsize(file_path) if file_size is None else file_size
		prefix = b"".join(
			self.__get_multipart_field(boundary, name, str(value))
			for name, value in payload.items()
		)
		prefix += self.__get_multipart_file_header(boundary, file_field_name, file_name)
		suffix = f"\r\n--{boundary}--\r\n".encode()
		headers = {
			"Content-Type": f"multipart/form-data; boundary={boundary}",
		}
		body = MultipartFileStream(
			prefix=prefix,
			suffix=suffix,
			file_path=file_path,
			file_offset=file_offset,
			file_size=file_size,
			chunk_size=self.__file_split_chunk_size_bytes,
		)

		try:
			resp = requests.post(url, data=body, headers=headers, timeout=timeout)
		finally:
			body.close()

		try:
			return resp.json()
		except requests.JSONDecodeError:
			self.__logger.error(
				f"Telegram returned non-json response, status={resp.status_code}, "
				f"content_type={resp.headers.get('content-type')}, body={resp.text[:500]}"
			)
			raise SendError("Telegram returned non-json response")

	def __send_audio_file(self, chat_id: int, file_path: str, file_name: str) -> None:
		payload = {"chat_id": chat_id, "title": file_name, "parse_mode": "HTML"}
		url = f"{self.__base_url}{self.__bot_key}/sendAudio"
		timeout = self.__telegram_options.audio_timeout_seconds

		resp = self.__post_multipart_file(
			url=url,
			payload=payload,
			file_field_name="audio",
			file_path=file_path,
			file_name=file_name,
			timeout=timeout,
		)
		self.__logger.info(resp)

		if not resp["ok"]:
			self.__logger.warning(resp)
			raise SendError(f"Could not send audio, Telegram: {resp['description']}")

	def __send_video_file(self, chat_id: int, file_path: str, file_name: str) -> None:
		payload = {"chat_id": chat_id, "title": file_name, "parse_mode": "HTML"}
		url = f"{self.__base_url}{self.__bot_key}/sendVideo"
		timeout = self.__telegram_options.video_timeout_seconds

		resp = self.__post_multipart_file(
			url=url,
			payload=payload,
			file_field_name="video",
			file_path=file_path,
			file_name=file_name,
			timeout=timeout,
		)
		self.__logger.info(resp)

		if not resp["ok"]:
			self.__logger.warning(resp)
			raise SendError(f"Could not send video, Telegram: {resp['description']}")

	def __send_split_audio(self, chat_id: int, file_path: str, file_name: str) -> None:
		segment_files = self.__split_media_file(file_path, file_name)
		for segment_file_path, segment_file_name in segment_files:
			self.__send_audio_file(chat_id, segment_file_path, segment_file_name)

	def __send_split_video(self, chat_id: int, file_path: str, file_name: str) -> None:
		segment_files = self.__split_media_file(file_path, file_name)
		for segment_file_path, segment_file_name in segment_files:
			self.__send_video_file(chat_id, segment_file_path, segment_file_name)

	def send_text(self, chat_id: int, text: str) -> None:
		try:
			payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}

			url = f"{self.__base_url}{self.__bot_key}/sendMessage"
			timeout = self.__telegram_options.text_timeout_seconds

			resp = requests.post(url, data=payload, timeout=timeout).json()
			self.__logger.info(resp)

			if not resp["ok"]:
				self.__logger.warning(resp)
				raise SendError(f"Could not send message, Telegram: {resp['description']}")

		except Exception:
			self.__logger.error("Unknown error", exc_info=True)
			raise SendError()

	def send_audio(self, chat_id: int, file_path: str, file_name: str, remove=False) -> None:
		try:
			if os.path.getsize(file_path) > self.__max_telegram_upload_size_bytes:
				self.__send_split_audio(chat_id=chat_id, file_path=file_path, file_name=file_name)
				return

			self.__send_audio_file(chat_id, file_path, file_name)

		except requests.Timeout:
			self.__logger.warning("Could not send audio, timeout")
			raise SendError("Could not send audio, timeout")

		except requests.ConnectionError:
			self.__logger.warning("Could not send audio, timeout")
			raise SendError("Could not send audio, timeout")

		except Exception:
			self.__logger.error("Unknown error", exc_info=True)
			raise SendError("Could not sent audio")

		finally:
			if remove:
				self.__delete_file_folder(file_path)

	def send_video(self, chat_id: int, file_path: str, file_name: str, remove=False) -> None:
		try:
			if os.path.getsize(file_path) > self.__max_telegram_upload_size_bytes:
				self.__send_split_video(chat_id=chat_id, file_path=file_path, file_name=file_name)
				return

			self.__send_video_file(chat_id, file_path, file_name)

		except requests.Timeout:
			self.__logger.warning("Could not send video, timeout")
			raise SendError("Could not send video, timeout")

		except requests.ConnectionError:
			self.__logger.warning("Could not send video, timeout")
			raise SendError("Could not send video, timeout")

		except Exception:
			self.__logger.error("Unknown error", exc_info=True)
			raise SendError("Could not sent video")

		finally:
			if remove:
				self.__delete_file_folder(file_path)
