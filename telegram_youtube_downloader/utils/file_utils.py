import os
import shutil
import logging


class FileUtils:
	@staticmethod
	def delete_directory(directory_path: str, logger: logging.Logger) -> None:
		logger.info(f"Deleting directory {directory_path}")

		try:
			shutil.rmtree(directory_path)
		except FileNotFoundError:
			logger.info(f"Directory already deleted: {directory_path}")
			return
		except Exception:
			logger.error(f"Could not delete directory: {directory_path}", exc_info=True)
			return

		if os.path.exists(directory_path):
			logger.error(f"Directory still exists after delete attempt: {directory_path}")
		else:
			logger.info(f"Directory deleted: {directory_path}")
