import os
from aiodav import Client
from typing import List, Dict
import tempfile
import sys
import pymupdf
import logging
pymupdf.set_messages(pylogging_level=logging.WARNING + 1) # disable unnecessary logging

import pymupdf4llm
from presidio_filter import PII_filter
import asyncio

class NextCloudClient:
    def __init__(self, share_host: str, share_token: str, pii_filter: PII_filter):
        """Initialize NextCloud client with the share URL."""
        self.webdav_options = {
            'hostname': share_host,
            'token': share_token,  # Token from the share URL
            'root': 'public.php/dav/files/'+share_token,
        }
        self.filter = pii_filter
        self._client = None
        # Adding an asyncio Lock to serialize thread-unsafe PDF processing
        self._pdf_lock = asyncio.Lock()
        
    def _get_client(self) -> Client:
        if self._client is None:
            self._client = Client(**self.webdav_options)
        return self._client
        
    async def list_files(self) -> List[str]:
        """List all files in the shared folder."""
        try:
            client = self._get_client()
            files = await client.list()
            return [item for item in files if not item.endswith('/')]
        except Exception as e:
            sys.stderr.write(f"Error listing files: {e}")
            return ["error, unable to read files"]
        
    async def list_files_metadata(self) -> List[Dict]:
        """A more detaild list of the files in the folder. Includes things like 
        size, modification date, etag, type, and absolute path."""
        try:
            client = self._get_client()
            files = await client.list(get_info=True)
            
            filtered_files = [
                {k: v for k, v in item.items() if k not in ('name', 'created', 'isdir')}
                for item in files if not item.get('isdir')
            ]
            return filtered_files
        except Exception as e:
            sys.stderr.write(f"Error listing file metadata: {e}")
            return ["error, unable to read file metadata"]

    def _convert_pdf(self, path: str) -> str:
        """Isolated structural wrapper for CPU bound document extractions."""
        return pymupdf4llm.to_markdown(path, show_progress=False)

    async def read_file(self, file_path: str, client_id: str = "default_client") -> str:
        """Read the content of a file from NextCloud."""
        temp_dir = tempfile.gettempdir()
        temp_name = os.path.join(temp_dir, f"mcp_{os.path.basename(file_path)}")
        
        try:
            client = self._get_client()
            await client.download_file(file_path, temp_name)
            if file_path.endswith('.pdf'):
                # pymupdf4llm is on the heavier side and isn't thread safe.
                # TODO: Look into maybe swapping for another alternativ.
                async with self._pdf_lock:
                    content_raw = await asyncio.to_thread(self._convert_pdf, temp_name)
            else:
                with open(temp_name, 'r', encoding='utf-8', errors='ignore') as f:
                    content_raw = f.read()
                        
            # Remove personal information from content.
            return await self.filter.apply_filter(content_raw=content_raw, client_id=client_id)
        
        except Exception as e:
            sys.stderr.write(f"Error reading file {file_path}: {e}")
            return "Error when reading file."
        finally:
            if os.path.exists(temp_name):
                try:
                    os.unlink(temp_name)
                except Exception as e:
                    sys.stderr.write(f"Failed to clear disk resource {temp_name}: {e}\n")
        
    async def build_context(self, client_id: str = "default_client") -> Dict[str, str]:
        """Build context from all files in the shared folder."""
        files = await self.list_files()
        if not files or "error, unable to read files" in files:
            return {}
            
        # Run all file processing routines concurrently using asyncio.gather
        tasks = [self.read_file(f, client_id=client_id) for f in files]
        contents = await asyncio.gather(*tasks)
        
        return dict(zip(files, contents))