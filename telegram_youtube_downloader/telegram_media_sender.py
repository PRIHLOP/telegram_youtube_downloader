import os
import uuid
import logging
import pathlib

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
	__max_telegram_upload_size_bytes = 200 * 1024 * 1024
	__file_split_chunk_size_bytes = 8 * 1024 * 1024
	__multipart_content_type = "application/octet-stream"

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

	def __get_split_file_name(self, file_name: str, part_number: int, part_count: int) -> str:
		file_path = pathlib.Path(file_name)
		part_suffix = f".part{part_number:03d}-of-{part_count:03d}"

		return f"{file_path.stem}{part_suffix}{file_path.suffix}"

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

	def __get_part_count(self, file_path: str) -> int:
		file_size = os.path.getsize(file_path)
		return (
			file_size + self.__max_telegram_upload_size_bytes - 1
		) // self.__max_telegram_upload_size_bytes

	def __send_document(
		self,
		chat_id: int,
		file_path: str,
		file_name: str,
		file_offset: int = 0,
		file_size: "int | None" = None,
	) -> None:
		payload = {"chat_id": chat_id, "caption": file_name, "parse_mode": "HTML"}
		url = f"{self.__base_url}{self.__bot_key}/sendDocument"
		timeout = self.__telegram_options.video_timeout_seconds

		resp = self.__post_multipart_file(
			url=url,
			payload=payload,
			file_field_name="document",
			file_path=file_path,
			file_name=file_name,
			timeout=timeout,
			file_offset=file_offset,
			file_size=file_size,
		)
		self.__logger.info(resp)

		if not resp["ok"]:
			self.__logger.warning(resp)
			raise SendError(f"Could not send document, Telegram: {resp['description']}")

	def __send_split_file(self, chat_id: int, file_path: str, file_name: str) -> None:
		file_size = os.path.getsize(file_path)
		part_count = self.__get_part_count(file_path)

		self.__logger.info(
			f"Sending file '{file_path}' in {part_count} parts, "
			f"max part size {self.__max_telegram_upload_size_bytes} bytes"
		)

		for part_number in range(1, part_count + 1):
			file_offset = (part_number - 1) * self.__max_telegram_upload_size_bytes
			part_size = min(
				self.__max_telegram_upload_size_bytes,
				file_size - file_offset,
			)
			part_file_name = self.__get_split_file_name(file_name, part_number, part_count)

			self.__send_document(
				chat_id=chat_id,
				file_path=file_path,
				file_name=part_file_name,
				file_offset=file_offset,
				file_size=part_size,
			)

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
				self.__send_split_file(chat_id=chat_id, file_path=file_path, file_name=file_name)
				return

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
				self.__send_split_file(chat_id=chat_id, file_path=file_path, file_name=file_name)
				return

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
