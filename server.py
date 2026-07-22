import os
import sys
import asyncio
import traceback
import copy
import inspect

import nodes
import folder_paths
import execution
import uuid
import urllib
import json
import glob
import struct
import ssl
import socket
import ipaddress
from PIL import Image, ImageOps
from PIL.PngImagePlugin import PngInfo
from io import BytesIO

import aiohttp
from aiohttp import web
import logging
from dataclasses import dataclass

import mimetypes
from comfy.cli_args import args
import comfy.utils
import comfy.model_management
from comfy_api import feature_flags
import node_helpers
from comfyui_version import __version__
from app.frontend_management import FrontendManager
from comfy_api.internal import _ComfyNodeInternal

from app.user_manager import UserManager
from app.model_manager import ModelFileManager
from app.custom_node_manager import CustomNodeManager
from app.subgraph_manager import SubgraphManager
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union
from api_server.routes.internal.internal_routes import InternalRoutes
from protocol import BinaryEventTypes


DEFAULT_HISTORY_PROVIDER_TIMEOUT = 0.5
PROMPT_ADMISSION_FIELD = "prompt_enqueue_admission_id"


class _DuplicateSocketJsonMember(ValueError):
    pass


def _reject_duplicate_socket_json_members(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateSocketJsonMember(key)
        value[key] = item
    return value


@dataclass
class PromptSubmitContext:
    request: web.Request
    original_json: Dict[str, Any]
    prompt: Dict[str, Any]
    prompt_id: str
    number: float
    outputs_to_execute: Optional[List[str]]
    extra_data: Dict[str, Any]
    client_id: Optional[str]
    partial_execution_targets: Optional[List[str]]

# Import cache control middleware
from middleware.cache_middleware import cache_control

async def send_socket_catch_exception(function, message):
    try:
        await function(message)
    except (aiohttp.ClientError, aiohttp.ClientPayloadError, ConnectionResetError, BrokenPipeError, ConnectionError) as err:
        logging.warning("send error: {}".format(err))

# Track deprecated paths that have been warned about to only warn once per file
_deprecated_paths_warned = set()

@web.middleware
async def deprecation_warning(request: web.Request, handler):
    """Middleware to warn about deprecated frontend API paths"""
    path = request.path

    if path.startswith("/scripts/ui") or path.startswith("/extensions/core/"):
        # Only warn once per unique file path
        if path not in _deprecated_paths_warned:
            _deprecated_paths_warned.add(path)
            logging.warning(
                f"[DEPRECATION WARNING] Detected import of deprecated legacy API: {path}. "
                f"This is likely caused by a custom node extension using outdated APIs. "
                f"Please update your extensions or contact the extension author for an updated version."
            )

    response: web.Response = await handler(request)
    return response


@web.middleware
async def compress_body(request: web.Request, handler):
    accept_encoding = request.headers.get("Accept-Encoding", "")
    response: web.Response = await handler(request)
    if not isinstance(response, web.Response):
        return response
    if response.content_type not in ["application/json", "text/plain"]:
        return response
    if response.body and "gzip" in accept_encoding:
        response.enable_compression()
    return response


def create_cors_middleware(allowed_origin: str):
    @web.middleware
    async def cors_middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            # Pre-flight request. Reply successfully:
            response = web.Response()
        else:
            response = await handler(request)

        response.headers['Access-Control-Allow-Origin'] = allowed_origin
        response.headers['Access-Control-Allow-Methods'] = 'POST, GET, DELETE, PUT, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        return response

    return cors_middleware

def is_loopback(host):
    if host is None:
        return False
    try:
        if ipaddress.ip_address(host).is_loopback:
            return True
        else:
            return False
    except:
        pass

    loopback = False
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            r = socket.getaddrinfo(host, None, family, socket.SOCK_STREAM)
            for family, _, _, _, sockaddr in r:
                if not ipaddress.ip_address(sockaddr[0]).is_loopback:
                    return loopback
                else:
                    loopback = True
        except socket.gaierror:
            pass

    return loopback


def create_origin_only_middleware():
    @web.middleware
    async def origin_only_middleware(request: web.Request, handler):
        #this code is used to prevent the case where a random website can queue comfy workflows by making a POST to 127.0.0.1 which browsers don't prevent for some dumb reason.
        #in that case the Host and Origin hostnames won't match
        #I know the proper fix would be to add a cookie but this should take care of the problem in the meantime
        if 'Host' in request.headers and 'Origin' in request.headers:
            host = request.headers['Host']
            origin = request.headers['Origin']
            host_domain = host.lower()
            parsed = urllib.parse.urlparse(origin)
            origin_domain = parsed.netloc.lower()
            host_domain_parsed = urllib.parse.urlsplit('//' + host_domain)

            #limit the check to when the host domain is localhost, this makes it slightly less safe but should still prevent the exploit
            loopback = is_loopback(host_domain_parsed.hostname)

            if parsed.port is None: #if origin doesn't have a port strip it from the host to handle weird browsers, same for host
                host_domain = host_domain_parsed.hostname
            if host_domain_parsed.port is None:
                origin_domain = parsed.hostname

            if loopback and host_domain is not None and origin_domain is not None and len(host_domain) > 0 and len(origin_domain) > 0:
                if host_domain != origin_domain:
                    logging.warning("WARNING: request with non matching host and origin {} != {}, returning 403".format(host_domain, origin_domain))
                    return web.Response(status=403)

        if request.method == "OPTIONS":
            response = web.Response()
        else:
            response = await handler(request)

        return response

    return origin_only_middleware

class PromptServer():
    def __init__(self, loop):
        PromptServer.instance = self

        mimetypes.init()
        mimetypes.add_type('application/javascript; charset=utf-8', '.js')
        mimetypes.add_type('image/webp', '.webp')

        self.user_manager = UserManager()
        self.model_file_manager = ModelFileManager()
        self.custom_node_manager = CustomNodeManager()
        self.subgraph_manager = SubgraphManager()
        self.internal_routes = InternalRoutes(self)
        self.supports = ["custom_nodes_from_web"]
        self.prompt_queue = execution.PromptQueue(self)
        self.loop = loop
        self.messages = asyncio.Queue()
        self.client_session:Optional[aiohttp.ClientSession] = None
        self.number = 0

        middlewares = [cache_control, deprecation_warning]
        if args.enable_compress_response_body:
            middlewares.append(compress_body)

        if args.enable_cors_header:
            middlewares.append(create_cors_middleware(args.enable_cors_header))
        else:
            middlewares.append(create_origin_only_middleware())

        max_upload_size = round(args.max_upload_size * 1024 * 1024)
        self.app = web.Application(client_max_size=max_upload_size, middlewares=middlewares)
        self.sockets = dict()
        self.sockets_metadata = dict()
        self._socket_session_lock = asyncio.Lock()
        self._socket_generation_counter = 0
        self._socket_sessions: Dict[str, Dict[str, Any]] = {}
        self._socket_message_handlers: Dict[str, Callable[..., Awaitable[None]]] = {}
        self._socket_lifecycle_callbacks: List[Callable[[str, str, int], None]] = []
        self.web_root = (
            FrontendManager.init_frontend(args.front_end_version)
            if args.front_end_root is None
            else args.front_end_root
        )
        logging.info(f"[Prompt Server] web root: {self.web_root}")
        routes = web.RouteTableDef()
        self.routes = routes
        self.last_node_id = None
        self.client_id = None

        self.on_prompt_handlers = []
        self.on_prompt_submit_handlers: List[Callable[[PromptSubmitContext], Awaitable[Optional[web.StreamResponse]]]] = []
        self.prompt_admission_provider = None
        self.history_providers: List[Callable[[Optional[str], Optional[int]], Awaitable[Optional[Dict[str, Any]]]]] = []
        self.history_provider_timeout = DEFAULT_HISTORY_PROVIDER_TIMEOUT

        @routes.get('/ws')
        async def websocket_handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            sid = request.rel_url.query.get('clientId', '')
            if not sid:
                sid = uuid.uuid4().hex
            generation = await self._install_socket_generation(sid, ws)

            try:
                # Send initial state to the new client
                initial_delivery = await self.send_json_exact(
                    sid,
                    generation,
                    ws,
                    "status",
                    {"status": self.get_queue_info(), "sid": sid},
                )
                if initial_delivery != "delivered":
                    return ws
                # On reconnect if we are the currently executing client send the current node
                if self.client_id == sid and self.last_node_id is not None:
                    await self.send_json_exact(
                        sid,
                        generation,
                        ws,
                        "executing",
                        {"node": self.last_node_id},
                    )

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.ERROR:
                        logging.warning('ws connection closed with exception %s' % ws.exception())
                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            accepted = await self._handle_socket_text(
                                sid, generation, ws, msg.data,
                            )
                            if not accepted:
                                await self.close_socket_generation(sid, generation, ws)
                                break
                        except Exception as e:
                            logging.error(f"Error processing WebSocket message: {e}")
                            await self.close_socket_generation(sid, generation, ws)
                            break
            finally:
                await self._remove_socket_generation(sid, generation, ws)
            return ws

        @routes.get("/")
        async def get_root(request):
            response = web.FileResponse(os.path.join(self.web_root, "index.html"))
            response.headers['Cache-Control'] = 'no-cache'
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            return response

        @routes.get("/embeddings")
        def get_embeddings(request):
            embeddings = folder_paths.get_filename_list("embeddings")
            return web.json_response(list(map(lambda a: os.path.splitext(a)[0], embeddings)))

        @routes.get("/models")
        def list_model_types(request):
            model_types = list(folder_paths.folder_names_and_paths.keys())

            return web.json_response(model_types)

        @routes.get("/models/{folder}")
        async def get_models(request):
            folder = request.match_info.get("folder", None)
            if not folder in folder_paths.folder_names_and_paths:
                return web.Response(status=404)
            files = folder_paths.get_filename_list(folder)
            return web.json_response(files)

        @routes.get("/extensions")
        async def get_extensions(request):
            files = glob.glob(os.path.join(
                glob.escape(self.web_root), 'extensions/**/*.js'), recursive=True)

            extensions = list(map(lambda f: "/" + os.path.relpath(f, self.web_root).replace("\\", "/"), files))

            for name, dir in nodes.EXTENSION_WEB_DIRS.items():
                files = glob.glob(os.path.join(glob.escape(dir), '**/*.js'), recursive=True)
                extensions.extend(list(map(lambda f: "/extensions/" + urllib.parse.quote(
                    name) + "/" + os.path.relpath(f, dir).replace("\\", "/"), files)))

            return web.json_response(extensions)

        def get_dir_by_type(dir_type):
            if dir_type is None:
                dir_type = "input"

            if dir_type == "input":
                type_dir = folder_paths.get_input_directory()
            elif dir_type == "temp":
                type_dir = folder_paths.get_temp_directory()
            elif dir_type == "output":
                type_dir = folder_paths.get_output_directory()

            return type_dir, dir_type

        def compare_image_hash(filepath, image):
            hasher = node_helpers.hasher()

            # function to compare hashes of two images to see if it already exists, fix to #3465
            if os.path.exists(filepath):
                a = hasher()
                b = hasher()
                with open(filepath, "rb") as f:
                    a.update(f.read())
                    b.update(image.file.read())
                    image.file.seek(0)
                return a.hexdigest() == b.hexdigest()
            return False

        def image_upload(post, image_save_function=None):
            image = post.get("image")
            overwrite = post.get("overwrite")
            image_is_duplicate = False

            image_upload_type = post.get("type")
            upload_dir, image_upload_type = get_dir_by_type(image_upload_type)

            if image and image.file:
                filename = image.filename
                if not filename:
                    return web.Response(status=400)

                subfolder = post.get("subfolder", "")
                full_output_folder = os.path.join(upload_dir, os.path.normpath(subfolder))
                filepath = os.path.abspath(os.path.join(full_output_folder, filename))

                if os.path.commonpath((upload_dir, filepath)) != upload_dir:
                    return web.Response(status=400)

                if not os.path.exists(full_output_folder):
                    os.makedirs(full_output_folder)

                split = os.path.splitext(filename)

                if overwrite is not None and (overwrite == "true" or overwrite == "1"):
                    pass
                else:
                    i = 1
                    while os.path.exists(filepath):
                        if compare_image_hash(filepath, image): #compare hash to prevent saving of duplicates with same name, fix for #3465
                            image_is_duplicate = True
                            break
                        filename = f"{split[0]} ({i}){split[1]}"
                        filepath = os.path.join(full_output_folder, filename)
                        i += 1

                if not image_is_duplicate:
                    if image_save_function is not None:
                        image_save_function(image, post, filepath)
                    else:
                        with open(filepath, "wb") as f:
                            f.write(image.file.read())

                return web.json_response({"name" : filename, "subfolder": subfolder, "type": image_upload_type})
            else:
                return web.Response(status=400)

        @routes.post("/upload/image")
        async def upload_image(request):
            post = await request.post()
            return image_upload(post)


        @routes.post("/upload/mask")
        async def upload_mask(request):
            post = await request.post()

            def image_save_function(image, post, filepath):
                original_ref = json.loads(post.get("original_ref"))
                filename, output_dir = folder_paths.annotated_filepath(original_ref['filename'])

                if not filename:
                    return web.Response(status=400)

                # validation for security: prevent accessing arbitrary path
                if filename[0] == '/' or '..' in filename:
                    return web.Response(status=400)

                if output_dir is None:
                    type = original_ref.get("type", "output")
                    output_dir = folder_paths.get_directory_by_type(type)

                if output_dir is None:
                    return web.Response(status=400)

                if original_ref.get("subfolder", "") != "":
                    full_output_dir = os.path.join(output_dir, original_ref["subfolder"])
                    if os.path.commonpath((os.path.abspath(full_output_dir), output_dir)) != output_dir:
                        return web.Response(status=403)
                    output_dir = full_output_dir

                file = os.path.join(output_dir, filename)

                if os.path.isfile(file):
                    with Image.open(file) as original_pil:
                        metadata = PngInfo()
                        if hasattr(original_pil,'text'):
                            for key in original_pil.text:
                                metadata.add_text(key, original_pil.text[key])
                        original_pil = original_pil.convert('RGBA')
                        mask_pil = Image.open(image.file).convert('RGBA')

                        # alpha copy
                        new_alpha = mask_pil.getchannel('A')
                        original_pil.putalpha(new_alpha)
                        original_pil.save(filepath, compress_level=4, pnginfo=metadata)

            return image_upload(post, image_save_function)

        @routes.get("/view")
        async def view_image(request):
            if "filename" in request.rel_url.query:
                filename = request.rel_url.query["filename"]
                filename, output_dir = folder_paths.annotated_filepath(filename)

                if not filename:
                    return web.Response(status=400)

                # validation for security: prevent accessing arbitrary path
                if filename[0] == '/' or '..' in filename:
                    return web.Response(status=400)

                if output_dir is None:
                    type = request.rel_url.query.get("type", "output")
                    output_dir = folder_paths.get_directory_by_type(type)

                if output_dir is None:
                    return web.Response(status=400)

                if "subfolder" in request.rel_url.query:
                    full_output_dir = os.path.join(output_dir, request.rel_url.query["subfolder"])
                    if os.path.commonpath((os.path.abspath(full_output_dir), output_dir)) != output_dir:
                        return web.Response(status=403)
                    output_dir = full_output_dir

                filename = os.path.basename(filename)
                file = os.path.join(output_dir, filename)

                if os.path.isfile(file):
                    if 'preview' in request.rel_url.query:
                        with Image.open(file) as img:
                            preview_info = request.rel_url.query['preview'].split(';')
                            image_format = preview_info[0]
                            if image_format not in ['webp', 'jpeg'] or 'a' in request.rel_url.query.get('channel', ''):
                                image_format = 'webp'

                            quality = 90
                            if preview_info[-1].isdigit():
                                quality = int(preview_info[-1])

                            buffer = BytesIO()
                            if image_format in ['jpeg'] or request.rel_url.query.get('channel', '') == 'rgb':
                                img = img.convert("RGB")
                            img.save(buffer, format=image_format, quality=quality)
                            buffer.seek(0)

                            return web.Response(body=buffer.read(), content_type=f'image/{image_format}',
                                                headers={"Content-Disposition": f"filename=\"{filename}\""})

                    if 'channel' not in request.rel_url.query:
                        channel = 'rgba'
                    else:
                        channel = request.rel_url.query["channel"]

                    if channel == 'rgb':
                        with Image.open(file) as img:
                            if img.mode == "RGBA":
                                r, g, b, a = img.split()
                                new_img = Image.merge('RGB', (r, g, b))
                            else:
                                new_img = img.convert("RGB")

                            buffer = BytesIO()
                            new_img.save(buffer, format='PNG')
                            buffer.seek(0)

                            return web.Response(body=buffer.read(), content_type='image/png',
                                                headers={"Content-Disposition": f"filename=\"{filename}\""})

                    elif channel == 'a':
                        with Image.open(file) as img:
                            if img.mode == "RGBA":
                                _, _, _, a = img.split()
                            else:
                                a = Image.new('L', img.size, 255)

                            # alpha img
                            alpha_img = Image.new('RGBA', img.size)
                            alpha_img.putalpha(a)
                            alpha_buffer = BytesIO()
                            alpha_img.save(alpha_buffer, format='PNG')
                            alpha_buffer.seek(0)

                            return web.Response(body=alpha_buffer.read(), content_type='image/png',
                                                headers={"Content-Disposition": f"filename=\"{filename}\""})
                    else:
                        # Get content type from mimetype, defaulting to 'application/octet-stream'
                        content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'

                        # For security, force certain mimetypes to download instead of display
                        if content_type in {'text/html', 'text/html-sandboxed', 'application/xhtml+xml', 'text/javascript', 'text/css'}:
                            content_type = 'application/octet-stream'  # Forces download

                        return web.FileResponse(
                            file,
                            headers={
                                "Content-Disposition": f"filename=\"{filename}\"",
                                "Content-Type": content_type
                            }
                        )

            return web.Response(status=404)

        @routes.get("/view_metadata/{folder_name}")
        async def view_metadata(request):
            folder_name = request.match_info.get("folder_name", None)
            if folder_name is None:
                return web.Response(status=404)
            if not "filename" in request.rel_url.query:
                return web.Response(status=404)

            filename = request.rel_url.query["filename"]
            if not filename.endswith(".safetensors"):
                return web.Response(status=404)

            safetensors_path = folder_paths.get_full_path(folder_name, filename)
            if safetensors_path is None:
                return web.Response(status=404)
            out = comfy.utils.safetensors_header(safetensors_path, max_size=1024*1024)
            if out is None:
                return web.Response(status=404)
            dt = json.loads(out)
            if not "__metadata__" in dt:
                return web.Response(status=404)
            return web.json_response(dt["__metadata__"])

        @routes.get("/system_stats")
        async def system_stats(request):
            device = comfy.model_management.get_torch_device()
            device_name = comfy.model_management.get_torch_device_name(device)
            cpu_device = comfy.model_management.torch.device("cpu")
            ram_total = comfy.model_management.get_total_memory(cpu_device)
            ram_free = comfy.model_management.get_free_memory(cpu_device)
            vram_total, torch_vram_total = comfy.model_management.get_total_memory(device, torch_total_too=True)
            vram_free, torch_vram_free = comfy.model_management.get_free_memory(device, torch_free_too=True)
            required_frontend_version = FrontendManager.get_required_frontend_version()
            installed_templates_version = FrontendManager.get_installed_templates_version()
            required_templates_version = FrontendManager.get_required_templates_version()

            system_stats = {
                "system": {
                    "os": os.name,
                    "ram_total": ram_total,
                    "ram_free": ram_free,
                    "comfyui_version": __version__,
                    "required_frontend_version": required_frontend_version,
                    "installed_templates_version": installed_templates_version,
                    "required_templates_version": required_templates_version,
                    "python_version": sys.version,
                    "pytorch_version": comfy.model_management.torch_version,
                    "embedded_python": os.path.split(os.path.split(sys.executable)[0])[1] == "python_embeded",
                    "argv": sys.argv
                },
                "devices": [
                    {
                        "name": device_name,
                        "type": device.type,
                        "index": device.index,
                        "vram_total": vram_total,
                        "vram_free": vram_free,
                        "torch_vram_total": torch_vram_total,
                        "torch_vram_free": torch_vram_free,
                    }
                ]
            }
            return web.json_response(system_stats)

        @routes.get("/features")
        async def get_features(request):
            return web.json_response(feature_flags.get_server_features())

        @routes.get("/prompt")
        async def get_prompt(request):
            return web.json_response(self.get_queue_info())

        def node_info(node_class):
            obj_class = nodes.NODE_CLASS_MAPPINGS[node_class]
            if issubclass(obj_class, _ComfyNodeInternal):
                return obj_class.GET_NODE_INFO_V1()
            info = {}
            info['input'] = obj_class.INPUT_TYPES()
            info['input_order'] = {key: list(value.keys()) for (key, value) in obj_class.INPUT_TYPES().items()}
            info['output'] = obj_class.RETURN_TYPES
            info['output_is_list'] = obj_class.OUTPUT_IS_LIST if hasattr(obj_class, 'OUTPUT_IS_LIST') else [False] * len(obj_class.RETURN_TYPES)
            info['output_name'] = obj_class.RETURN_NAMES if hasattr(obj_class, 'RETURN_NAMES') else info['output']
            info['name'] = node_class
            info['display_name'] = nodes.NODE_DISPLAY_NAME_MAPPINGS[node_class] if node_class in nodes.NODE_DISPLAY_NAME_MAPPINGS.keys() else node_class
            info['description'] = obj_class.DESCRIPTION if hasattr(obj_class,'DESCRIPTION') else ''
            info['python_module'] = getattr(obj_class, "RELATIVE_PYTHON_MODULE", "nodes")
            info['category'] = 'sd'
            if hasattr(obj_class, 'OUTPUT_NODE') and obj_class.OUTPUT_NODE == True:
                info['output_node'] = True
            else:
                info['output_node'] = False

            if hasattr(obj_class, 'CATEGORY'):
                info['category'] = obj_class.CATEGORY

            if hasattr(obj_class, 'OUTPUT_TOOLTIPS'):
                info['output_tooltips'] = obj_class.OUTPUT_TOOLTIPS

            if getattr(obj_class, "DEPRECATED", False):
                info['deprecated'] = True
            if getattr(obj_class, "EXPERIMENTAL", False):
                info['experimental'] = True

            if hasattr(obj_class, 'API_NODE'):
                info['api_node'] = obj_class.API_NODE
            return info

        @routes.get("/object_info")
        async def get_object_info(request):
            with folder_paths.cache_helper:
                out = {}
                for x in nodes.NODE_CLASS_MAPPINGS:
                    try:
                        out[x] = node_info(x)
                    except Exception:
                        logging.error(f"[ERROR] An error occurred while retrieving information for the '{x}' node.")
                        logging.error(traceback.format_exc())
                return web.json_response(out)

        @routes.get("/object_info/{node_class}")
        async def get_object_info_node(request):
            node_class = request.match_info.get("node_class", None)
            out = {}
            if (node_class is not None) and (node_class in nodes.NODE_CLASS_MAPPINGS):
                out[node_class] = node_info(node_class)
            return web.json_response(out)

        @routes.get("/history")
        async def get_history(request):
            max_items_param = request.rel_url.query.get("max_items")
            max_items: Optional[int] = None
            if max_items_param is not None:
                try:
                    max_items = int(max_items_param)
                except ValueError:
                    return web.Response(status=400, text="max_items must be an integer")

            provider_history = await self.query_history(None, max_items)
            if provider_history is not None:
                return web.json_response(provider_history)

            offset = request.rel_url.query.get("offset", None)
            if offset is not None:
                offset = int(offset)
            else:
                offset = -1

            return web.json_response(self.prompt_queue.get_history(max_items=max_items, offset=offset))

        @routes.get("/history/{prompt_id}")
        async def get_history_prompt_id(request):
            prompt_id = request.match_info.get("prompt_id", None)
            max_items_param = request.rel_url.query.get("max_items")
            max_items: Optional[int] = None
            if max_items_param is not None:
                try:
                    max_items = int(max_items_param)
                except ValueError:
                    return web.Response(status=400, text="max_items must be an integer")

            provider_history = await self.query_history(prompt_id, max_items)
            if provider_history is not None:
                return web.json_response(provider_history)

            return web.json_response(self.prompt_queue.get_history(prompt_id=prompt_id, max_items=max_items))

        @routes.get("/queue")
        async def get_queue(request):
            queue_info = {}
            current_queue = self.prompt_queue.get_current_queue_volatile()
            queue_info['queue_running'] = current_queue[0]
            queue_info['queue_pending'] = current_queue[1]
            return web.json_response(queue_info)

        @routes.post("/prompt")
        async def post_prompt(request):
            return await self._post_prompt(request)

        @routes.post("/queue")
        async def post_queue(request):
            json_data =  await request.json()
            if "clear" in json_data:
                if json_data["clear"]:
                    self.prompt_queue.wipe_queue()
            if "delete" in json_data:
                to_delete = json_data['delete']
                for id_to_delete in to_delete:
                    delete_func = lambda a: a[1] == id_to_delete
                    self.prompt_queue.delete_queue_item(delete_func)

            return web.Response(status=200)

        @routes.post("/interrupt")
        async def post_interrupt(request):
            try:
                json_data = await request.json()
            except json.JSONDecodeError:
                json_data = {}

            # Check if a specific prompt_id was provided for targeted interruption
            prompt_id = json_data.get('prompt_id')
            if prompt_id:
                interrupted = self.prompt_queue.interrupt_running(
                    prompt_id, nodes.interrupt_processing
                )
                if interrupted:
                    logging.info(f"Interrupting prompt {prompt_id}")
                else:
                    logging.info(f"Prompt {prompt_id} is not currently running, skipping interrupt")
            else:
                # No prompt_id provided, do a global interrupt
                logging.info("Global interrupt (no prompt_id specified)")
                self.prompt_queue.interrupt_running(None, nodes.interrupt_processing)

            return web.Response(status=200)

        @routes.post("/free")
        async def post_free(request):
            json_data = await request.json()
            unload_models = json_data.get("unload_models", False)
            free_memory = json_data.get("free_memory", False)
            if unload_models:
                self.prompt_queue.set_flag("unload_models", unload_models)
            if free_memory:
                self.prompt_queue.set_flag("free_memory", free_memory)
            return web.Response(status=200)

        @routes.post("/history")
        async def post_history(request):
            json_data =  await request.json()
            if "clear" in json_data:
                if json_data["clear"]:
                    self.prompt_queue.wipe_history()
            if "delete" in json_data:
                to_delete = json_data['delete']
                for id_to_delete in to_delete:
                    self.prompt_queue.delete_history_item(id_to_delete)

            return web.Response(status=200)

    async def setup(self):
        timeout = aiohttp.ClientTimeout(total=None) # no timeout
        self.client_session = aiohttp.ClientSession(timeout=timeout)

    def add_routes(self):
        self.user_manager.add_routes(self.routes)
        self.model_file_manager.add_routes(self.routes)
        self.custom_node_manager.add_routes(self.routes, self.app, nodes.LOADED_MODULE_DIRS.items())
        self.subgraph_manager.add_routes(self.routes, nodes.LOADED_MODULE_DIRS.items())
        self.app.add_subapp('/internal', self.internal_routes.get_app())

        # Prefix every route with /api for easier matching for delegation.
        # This is very useful for frontend dev server, which need to forward
        # everything except serving of static files.
        # Currently both the old endpoints without prefix and new endpoints with
        # prefix are supported.
        api_routes = web.RouteTableDef()
        for route in self.routes:
            # Custom nodes might add extra static routes. Only process non-static
            # routes to add /api prefix.
            if isinstance(route, web.RouteDef):
                api_routes.route(route.method, "/api" + route.path)(route.handler, **route.kwargs)
        self.app.add_routes(api_routes)
        self.app.add_routes(self.routes)

        # Add routes from web extensions.
        for name, dir in nodes.EXTENSION_WEB_DIRS.items():
            self.app.add_routes([web.static('/extensions/' + name, dir)])

        workflow_templates_path = FrontendManager.templates_path()
        if workflow_templates_path:
            self.app.add_routes([
                web.static('/templates', workflow_templates_path)
            ])

        # Serve embedded documentation from the package
        embedded_docs_path = FrontendManager.embedded_docs_path()
        if embedded_docs_path:
            self.app.add_routes([
                web.static('/docs', embedded_docs_path)
            ])

        self.app.add_routes([
            web.static('/', self.web_root),
        ])

    def register_socket_message_handler(self, message_type, handler):
        if not isinstance(message_type, str) or not message_type or not callable(handler):
            raise ValueError("socket message handler registration is invalid")
        if message_type in self._socket_message_handlers:
            raise RuntimeError(f"socket message handler already registered: {message_type}")
        self._socket_message_handlers[message_type] = handler

    def register_socket_lifecycle_callback(self, callback):
        if not callable(callback):
            raise ValueError("socket lifecycle callback must be callable")
        if callback in self._socket_lifecycle_callbacks:
            raise RuntimeError("socket lifecycle callback already registered")
        self._socket_lifecycle_callbacks.append(callback)

    def _publish_socket_lifecycle(self, event, sid, generation):
        for callback in tuple(self._socket_lifecycle_callbacks):
            try:
                callback(event, sid, generation)
            except Exception:
                logging.exception("socket lifecycle callback failed")

    async def get_current_socket_generation(self, sid):
        async with self._socket_session_lock:
            record = self._socket_sessions.get(sid)
            if record is None or record["revoked"]:
                return None
            return record["generation"]

    async def try_begin_generation_bound_side_effect(
        self, sid, generation, socket_object, abort_token, transition_owner,
    ):
        async with self._socket_session_lock:
            record = self._socket_sessions.get(sid)
            generation_current = (
                record is not None
                and record["generation"] == generation
                and record["socket"] is socket_object
                and not record["revoked"]
            )
            return transition_owner.try_begin(
                generation_current,
                abort_observed=abort_token.aborted,
            )

    async def try_begin_generation_bound_post_guard_continuation(
        self, sid, generation, socket_object, abort_token, transition_owner,
    ):
        async with self._socket_session_lock:
            record = self._socket_sessions.get(sid)
            generation_current = (
                record is not None
                and record["generation"] == generation
                and record["socket"] is socket_object
                and not record["revoked"]
            )
            return transition_owner.try_begin_post_guard_continuation(
                generation_current,
                abort_observed=abort_token.aborted,
            )

    async def try_publish_generation_bound_terminal(
        self, sid, generation, socket_object, abort_token, transition_owner,
    ):
        async with self._socket_session_lock:
            record = self._socket_sessions.get(sid)
            generation_current = (
                record is not None
                and record["generation"] == generation
                and record["socket"] is socket_object
                and not record["revoked"]
            )
            return transition_owner.publish_terminal(
                generation_current,
                abort_observed=abort_token.aborted,
            )

    async def send_json_exact(self, sid, generation, socket_object, event, data, timeout=5.0):
        closed = False
        async with self._socket_session_lock:
            record = self._socket_sessions.get(sid)
            if (
                record is None
                or record["generation"] != generation
                or record["socket"] is not socket_object
                or record["revoked"]
            ):
                return "replaced"
            try:
                await asyncio.wait_for(
                    socket_object.send_json({"type": event, "data": data}),
                    timeout=timeout,
                )
            except Exception:
                record["revoked"] = True
                self._socket_sessions.pop(sid, None)
                if self.sockets.get(sid) is socket_object:
                    self.sockets.pop(sid, None)
                    self.sockets_metadata.pop(sid, None)
                closed = True
            else:
                return "delivered"
        if closed:
            if not socket_object.closed:
                await socket_object.close()
            self._publish_socket_lifecycle("disconnected", sid, generation)
        return "closed"

    async def close_socket_generation(self, sid, generation, socket_object):
        removed = False
        async with self._socket_session_lock:
            record = self._socket_sessions.get(sid)
            if (
                record is not None
                and record["generation"] == generation
                and record["socket"] is socket_object
            ):
                record["revoked"] = True
                self._socket_sessions.pop(sid, None)
                if self.sockets.get(sid) is socket_object:
                    self.sockets.pop(sid, None)
                    self.sockets_metadata.pop(sid, None)
                removed = True
        if not socket_object.closed:
            await socket_object.close()
        if removed:
            self._publish_socket_lifecycle("disconnected", sid, generation)
        return removed

    async def _install_socket_generation(self, sid, socket_object):
        replaced = None
        async with self._socket_session_lock:
            previous = self._socket_sessions.get(sid)
            if previous is not None:
                previous["revoked"] = True
                replaced = (previous["generation"], previous["socket"])
            self._socket_generation_counter += 1
            generation = self._socket_generation_counter
            self._socket_sessions[sid] = {
                "generation": generation,
                "socket": socket_object,
                "revoked": False,
                "feature_flags_set": False,
            }
            self.sockets[sid] = socket_object
            self.sockets_metadata[sid] = {
                "feature_flags": {},
                "socket_generation": generation,
            }
        if replaced is not None:
            replaced_generation, replaced_socket = replaced
            if not replaced_socket.closed:
                await replaced_socket.close()
            self._publish_socket_lifecycle("disconnected", sid, replaced_generation)
        self._publish_socket_lifecycle("connected", sid, generation)
        return generation

    async def _remove_socket_generation(self, sid, generation, socket_object):
        removed = False
        async with self._socket_session_lock:
            record = self._socket_sessions.get(sid)
            if (
                record is not None
                and record["generation"] == generation
                and record["socket"] is socket_object
            ):
                record["revoked"] = True
                self._socket_sessions.pop(sid, None)
                if self.sockets.get(sid) is socket_object:
                    self.sockets.pop(sid, None)
                    self.sockets_metadata.pop(sid, None)
                removed = True
        if removed:
            self._publish_socket_lifecycle("disconnected", sid, generation)

    async def _handle_socket_text(self, sid, generation, socket_object, raw_message):
        try:
            message = json.loads(raw_message, object_pairs_hook=_reject_duplicate_socket_json_members)
        except (json.JSONDecodeError, _DuplicateSocketJsonMember):
            return False
        if not isinstance(message, dict) or set(message) != {"type", "data"}:
            return False
        message_type = message.get("type")
        data = message.get("data")
        if not isinstance(message_type, str) or not isinstance(data, dict):
            return False
        feature_flags_settled_now = False
        async with self._socket_session_lock:
            record = self._socket_sessions.get(sid)
            if (
                record is None
                or record["generation"] != generation
                or record["socket"] is not socket_object
                or record["revoked"]
            ):
                return False
            if not record["feature_flags_set"]:
                if message_type != "feature_flags":
                    return False
                record["feature_flags_set"] = True
                self.sockets_metadata[sid]["feature_flags"] = dict(data)
                feature_flags_settled_now = True
                handler = None
            else:
                handler = self._socket_message_handlers.get(message_type)
        if message_type == "feature_flags":
            if not feature_flags_settled_now:
                return False
            disposition = await self.send_json_exact(
                sid, generation, socket_object,
                "feature_flags", feature_flags.get_server_features(),
            )
            return disposition == "delivered"
        if handler is None:
            return True
        await handler(sid, generation, socket_object, data)
        return True

    def get_queue_info(self):
        prompt_info = {}
        exec_info = {}
        exec_info['queue_remaining'] = self.prompt_queue.get_tasks_remaining()
        prompt_info['exec_info'] = exec_info
        return prompt_info

    async def send(self, event, data, sid=None):
        if event == BinaryEventTypes.UNENCODED_PREVIEW_IMAGE:
            await self.send_image(data, sid=sid)
        elif event == BinaryEventTypes.PREVIEW_IMAGE_WITH_METADATA:
            # data is (preview_image, metadata)
            preview_image, metadata = data
            await self.send_image_with_metadata(preview_image, metadata, sid=sid)
        elif isinstance(data, (bytes, bytearray)):
            await self.send_bytes(event, data, sid)
        else:
            await self.send_json(event, data, sid)

    def encode_bytes(self, event, data):
        if not isinstance(event, int):
            raise RuntimeError(f"Binary event types must be integers, got {event}")

        packed = struct.pack(">I", event)
        message = bytearray(packed)
        message.extend(data)
        return message

    async def send_image(self, image_data, sid=None):
        image_type = image_data[0]
        image = image_data[1]
        max_size = image_data[2]
        if max_size is not None:
            if hasattr(Image, 'Resampling'):
                resampling = Image.Resampling.BILINEAR
            else:
                resampling = Image.Resampling.LANCZOS

            image = ImageOps.contain(image, (max_size, max_size), resampling)
        type_num = 1
        if image_type == "JPEG":
            type_num = 1
        elif image_type == "PNG":
            type_num = 2

        bytesIO = BytesIO()
        header = struct.pack(">I", type_num)
        bytesIO.write(header)
        image.save(bytesIO, format=image_type, quality=95, compress_level=1)
        preview_bytes = bytesIO.getvalue()
        await self.send_bytes(BinaryEventTypes.PREVIEW_IMAGE, preview_bytes, sid=sid)

    async def send_image_with_metadata(self, image_data, metadata=None, sid=None):
        image_type = image_data[0]
        image = image_data[1]
        max_size = image_data[2]
        if max_size is not None:
            if hasattr(Image, 'Resampling'):
                resampling = Image.Resampling.BILINEAR
            else:
                resampling = Image.Resampling.LANCZOS

            image = ImageOps.contain(image, (max_size, max_size), resampling)

        mimetype = "image/png" if image_type == "PNG" else "image/jpeg"

        # Prepare metadata
        if metadata is None:
            metadata = {}
        metadata["image_type"] = mimetype

        # Serialize metadata as JSON
        import json
        metadata_json = json.dumps(metadata).encode('utf-8')
        metadata_length = len(metadata_json)

        # Prepare image data
        bytesIO = BytesIO()
        image.save(bytesIO, format=image_type, quality=95, compress_level=1)
        image_bytes = bytesIO.getvalue()

        # Combine metadata and image
        combined_data = bytearray()
        combined_data.extend(struct.pack(">I", metadata_length))
        combined_data.extend(metadata_json)
        combined_data.extend(image_bytes)

        await self.send_bytes(BinaryEventTypes.PREVIEW_IMAGE_WITH_METADATA, combined_data, sid=sid)

    async def send_bytes(self, event, data, sid=None):
        message = self.encode_bytes(event, data)

        if sid is None:
            sockets = list(self.sockets.values())
            for ws in sockets:
                await send_socket_catch_exception(ws.send_bytes, message)
        elif sid in self.sockets:
            await send_socket_catch_exception(self.sockets[sid].send_bytes, message)

    async def send_json(self, event, data, sid=None):
        message = {"type": event, "data": data}

        if sid is None:
            sockets = list(self.sockets.values())
            for ws in sockets:
                await send_socket_catch_exception(ws.send_json, message)
        elif sid in self.sockets:
            await send_socket_catch_exception(self.sockets[sid].send_json, message)

    def send_sync(self, event, data, sid=None):
        self.loop.call_soon_threadsafe(
            self.messages.put_nowait, (event, data, sid))

    def queue_updated(self):
        self.send_sync("status", { "status": self.get_queue_info() })

    async def publish_loop(self):
        while True:
            msg = await self.messages.get()
            await self.send(*msg)

    async def start(self, address, port, verbose=True, call_on_start=None):
        await self.start_multi_address([(address, port)], call_on_start=call_on_start)

    async def start_multi_address(self, addresses, call_on_start=None, verbose=True):
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        ssl_ctx = None
        scheme = "http"
        if args.tls_keyfile and args.tls_certfile:
            ssl_ctx = ssl.SSLContext(protocol=ssl.PROTOCOL_TLS_SERVER, verify_mode=ssl.CERT_NONE)
            ssl_ctx.load_cert_chain(certfile=args.tls_certfile,
                                keyfile=args.tls_keyfile)
            scheme = "https"

        if verbose:
            logging.info("Starting server\n")
        for addr in addresses:
            address = addr[0]
            port = addr[1]
            site = web.TCPSite(runner, address, port, ssl_context=ssl_ctx)
            await site.start()

            if not hasattr(self, 'address'):
                self.address = address #TODO: remove this
                self.port = port

            if ':' in address:
                address_print = "[{}]".format(address)
            else:
                address_print = address

            if verbose:
                logging.info("To see the GUI go to: {}://{}:{}".format(scheme, address_print, port))

        if call_on_start is not None:
            call_on_start(scheme, self.address, self.port)

    def add_on_prompt_submit_handler(
        self,
        handler: Callable[[PromptSubmitContext], Awaitable[Optional[web.StreamResponse]]],
    ) -> None:
        self.on_prompt_submit_handlers.append(handler)

    def register_prompt_admission_provider(self, provider) -> None:
        if self.prompt_admission_provider is not None:
            raise RuntimeError("prompt admission provider already registered")
        required = ("matches", "has_admission", "consume", "build_sidecar", "terminalize_prequeue")
        if provider is None or any(not callable(getattr(provider, name, None)) for name in required):
            raise ValueError("prompt admission provider is invalid")
        self.prompt_admission_provider = provider

    @staticmethod
    def _prompt_submission_error(code, message, *, status=400):
        error = {
            "type": code,
            "message": message,
            "details": message,
            "extra_info": {},
        }
        return web.json_response({"error": error, "node_errors": {}}, status=status)

    async def _consume_prompt_admission(self, raw_json):
        provider = self.prompt_admission_provider
        has_admission = (
            provider.has_admission(raw_json)
            if provider is not None
            else PROMPT_ADMISSION_FIELD in raw_json
        )
        raw_matches = provider is not None and provider.matches(raw_json)
        if raw_matches and not has_admission:
            raise RuntimeError("prompt_admission_required")
        if has_admission and not raw_matches:
            raise RuntimeError("prompt_admission_not_applicable")
        if not raw_matches:
            return None
        return await provider.consume(raw_json)

    def _prompt_number(self, json_data):
        if "number" in json_data:
            return float(json_data["number"])
        number = self.number
        if json_data.get("front"):
            number = -number
        self.number += 1
        return number

    @staticmethod
    def _prompt_extra_data(json_data):
        extra_data = json_data.get("extra_data", {})
        if not isinstance(extra_data, dict):
            logging.warning("extra_data payload is not a dict; resetting to empty")
            extra_data = {}
        else:
            extra_data = copy.deepcopy(extra_data)
        if "client_id" in json_data:
            extra_data["client_id"] = json_data["client_id"]
        return extra_data

    async def _post_prompt(self, request):
        logging.info("got prompt")
        lease = None
        queue_committed = False
        provider = self.prompt_admission_provider
        try:
            raw_json = await request.json()
            if not isinstance(raw_json, dict):
                return self._prompt_submission_error("no_prompt", "No prompt provided")
            try:
                lease = await self._consume_prompt_admission(raw_json)
            except RuntimeError as error:
                if str(error) == "prompt_admission_required":
                    return self._prompt_submission_error(
                        "prompt_admission_required",
                        "Workbench prompt admission is required",
                    )
                if str(error) == "prompt_admission_not_applicable":
                    return self._prompt_submission_error(
                        "prompt_admission_not_applicable",
                        "Prompt admission cannot authorize an ordinary prompt",
                    )
                raise

            json_data = self.trigger_on_prompt(
                copy.deepcopy(raw_json), fail_closed=lease is not None
            )
            if not isinstance(json_data, dict) or "prompt" not in json_data:
                return self._prompt_submission_error("no_prompt", "No prompt provided")

            number = self._prompt_number(json_data)
            prompt_id = str(json_data.get("prompt_id", uuid.uuid4()))
            prompt = copy.deepcopy(json_data["prompt"])
            partial_targets = copy.deepcopy(
                json_data.get("partial_execution_targets")
            )
            valid = await execution.validate_prompt(
                prompt_id, prompt, partial_targets
            )
            if not valid[0]:
                logging.warning("invalid prompt: %s", valid[1])
                return web.json_response(
                    {"error": valid[1], "node_errors": valid[3]}, status=400
                )

            context = PromptSubmitContext(
                request=request,
                original_json=copy.deepcopy(json_data),
                prompt=prompt,
                prompt_id=prompt_id,
                number=number,
                outputs_to_execute=valid[2],
                extra_data=self._prompt_extra_data(json_data),
                client_id=json_data.get("client_id"),
                partial_execution_targets=partial_targets,
            )
            hook_response = await self.trigger_on_prompt_submit(
                context, fail_closed=lease is not None
            )
            if hook_response is not None:
                if lease is not None:
                    return self._prompt_submission_error(
                        "prompt_admission_hook_short_circuit",
                        "Admitted Workbench prompts cannot be short-circuited",
                    )
                return hook_response

            final_json = copy.deepcopy(context.original_json)
            if not isinstance(final_json, dict):
                return self._prompt_submission_error(
                    "prompt_transform_invalid", "Prompt transform returned invalid data"
                )
            final_prompt = copy.deepcopy(context.prompt)
            final_json["prompt"] = final_prompt
            final_matches = provider is not None and provider.matches(final_json)
            if lease is not None and not final_matches:
                return self._prompt_submission_error(
                    "prompt_admission_downgrade_rejected",
                    "An admitted Workbench prompt cannot become ordinary",
                )
            if lease is None and final_matches:
                return self._prompt_submission_error(
                    "prompt_admission_required",
                    "Hooks cannot introduce Workbench into an unadmitted prompt",
                )

            final_valid = await execution.validate_prompt(
                context.prompt_id,
                final_prompt,
                context.partial_execution_targets,
            )
            if not final_valid[0]:
                logging.warning("invalid final prompt: %s", final_valid[1])
                return web.json_response(
                    {"error": final_valid[1], "node_errors": final_valid[3]},
                    status=400,
                )

            item = (
                context.number,
                context.prompt_id,
                final_prompt,
                copy.deepcopy(context.extra_data),
                final_valid[2],
            )
            if lease is None:
                self.prompt_queue.put(item)
            else:
                sidecar = await provider.build_sidecar(lease, final_json)
                self.prompt_queue.put_with_workbench_sidecar(item, sidecar)
            queue_committed = True
            return web.json_response(
                {
                    "prompt_id": context.prompt_id,
                    "number": context.number,
                    "node_errors": final_valid[3],
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = getattr(error, "code", "prompt_submission_failed")
            status = getattr(error, "status", 400)
            logging.error("prompt submission failed: %s", error, exc_info=True)
            return self._prompt_submission_error(code, str(error), status=status)
        finally:
            if lease is not None and not queue_committed:
                provider.terminalize_prequeue(lease, "prequeue_rejected")

    async def trigger_on_prompt_submit(
        self, ctx: PromptSubmitContext, *, fail_closed: bool = False
    ) -> Optional[web.StreamResponse]:
        if not self.on_prompt_submit_handlers:
            return None

        for handler in list(self.on_prompt_submit_handlers):
            try:
                result = handler(ctx)
                if inspect.isawaitable(result):
                    result = await result
            except asyncio.CancelledError:
                raise
            except Exception:
                if fail_closed:
                    raise
                logging.error(
                    "on_prompt_submit handler %s raised an exception; continuing core queue",
                    getattr(handler, "__name__", repr(handler)),
                    exc_info=True,
                )
                continue

            if result is None:
                continue

            if not isinstance(result, web.StreamResponse):
                logging.warning(
                    "[ALCHEM] on_prompt_submit handler %s returned unsupported response type %s; ignoring",
                    getattr(handler, "__name__", repr(handler)),
                    type(result),
                )
                continue

            logging.info(
                "[ALCHEM] prompt submission short-circuited by handler %s",
                getattr(handler, "__name__", repr(handler)),
            )
            return result

        return None

    def add_history_provider(
        self,
        provider: Callable[[Optional[str], Optional[int]], Awaitable[Optional[Dict[str, Any]]]],
    ) -> None:
        self.history_providers.append(provider)

    def set_history_provider_timeout(self, timeout: Optional[float]) -> None:
        if timeout is None:
            self.history_provider_timeout = None
            return

        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("history provider timeout must be a positive float or None") from exc

        if timeout_value <= 0:
            raise ValueError("history provider timeout must be greater than zero")

        self.history_provider_timeout = timeout_value

    async def query_history(
        self, prompt_id: Optional[str], max_items: Optional[int]
    ) -> Optional[Dict[str, Any]]:
        if not self.history_providers:
            return None

        task_to_provider: Dict[asyncio.Task[Optional[Dict[str, Any]]], Callable[[Optional[str], Optional[int]], Awaitable[Optional[Dict[str, Any]]]]] = {}
        tasks: List[asyncio.Task[Optional[Dict[str, Any]]]] = []
        for provider in list(self.history_providers):
            task = asyncio.create_task(
                self._invoke_history_provider_with_timeout(provider, prompt_id, max_items)
            )
            task_to_provider[task] = provider
            tasks.append(task)

        aggregated: Optional[Dict[str, Any]] = None
        if not tasks:
            return None

        for completed in asyncio.as_completed(tasks):
            provider = task_to_provider.get(completed)
            try:
                payload = await completed
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.error(
                    "[ALCHEM] history provider %s raised an unexpected exception",
                    self._history_provider_display_name(provider),
                    exc_info=True,
                )
                continue

            if payload is None:
                continue

            if aggregated is None:
                aggregated = copy.deepcopy(payload)
                continue

            self._merge_history_payload(aggregated, payload, prompt_id)

        for task in tasks:
            task_to_provider.pop(task, None)

        if aggregated is None:
            return None

        self._apply_history_max_items(aggregated, prompt_id, max_items)
        logging.debug(
            "[ALCHEM] history aggregated via providers: prompt_id=%s, max_items=%s",
            prompt_id,
            max_items,
        )
        return aggregated

    async def _invoke_history_provider_with_timeout(
        self,
        provider: Callable[[Optional[str], Optional[int]], Awaitable[Optional[Dict[str, Any]]]],
        prompt_id: Optional[str],
        max_items: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        timeout = getattr(self, "history_provider_timeout", None)
        name = self._history_provider_display_name(provider)
        try:
            if timeout is None or timeout <= 0:
                return await self._invoke_history_provider(provider, prompt_id, max_items, name)
            return await asyncio.wait_for(
                self._invoke_history_provider(provider, prompt_id, max_items, name),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logging.warning(
                "[ALCHEM] history provider %s timed out after %.2fs",
                name,
                timeout,
            )
            return None

    async def _invoke_history_provider(
        self,
        provider: Callable[[Optional[str], Optional[int]], Awaitable[Optional[Dict[str, Any]]]],
        prompt_id: Optional[str],
        max_items: Optional[int],
        provider_name: str,
    ) -> Optional[Dict[str, Any]]:
        try:
            payload = provider(prompt_id, max_items)
            if inspect.isawaitable(payload):
                payload = await payload
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.error(
                "[ALCHEM] history provider %s raised an exception",
                provider_name,
                exc_info=True,
            )
            return None

        return self._normalize_history_payload(payload, prompt_id, provider_name)

    def _normalize_history_payload(
        self,
        payload: Optional[Dict[str, Any]],
        prompt_id: Optional[str],
        provider_name: str,
    ) -> Optional[Dict[str, Any]]:
        if payload is None:
            return None

        if not isinstance(payload, dict):
            logging.warning(
                "[ALCHEM] history provider %s returned non-dict payload %s",
                provider_name,
                type(payload),
            )
            return None

        if prompt_id is not None and prompt_id not in payload:
            # 允许 Provider 直接返回单条历史结构，自动包裹 prompt_id
            payload = {prompt_id: payload}

        return payload

    def _merge_history_payload(
        self,
        base: Dict[str, Any],
        extra: Dict[str, Any],
        prompt_id: Optional[str],
    ) -> None:
        for key, value in extra.items():
            if key not in base:
                base[key] = value
                continue

            if isinstance(base[key], dict) and isinstance(value, dict):
                self._merge_history_entry(base[key], value)

    def _merge_history_entry(self, base_entry: Dict[str, Any], new_entry: Dict[str, Any]) -> None:
        base_outputs = base_entry.get("outputs")
        new_outputs = new_entry.get("outputs")

        if isinstance(base_outputs, list) and isinstance(new_outputs, list):
            base_entry["outputs"] = base_outputs + [item for item in new_outputs if item not in base_outputs]
        elif isinstance(base_outputs, dict) and isinstance(new_outputs, dict):
            for output_key, output_value in new_outputs.items():
                base_outputs.setdefault(output_key, output_value)
        elif base_outputs is None and new_outputs is not None:
            base_entry["outputs"] = new_outputs

        base_meta = base_entry.get("meta")
        new_meta = new_entry.get("meta")
        if isinstance(new_meta, dict):
            if not isinstance(base_meta, dict):
                base_entry["meta"] = dict(new_meta)
            else:
                for meta_key, meta_value in new_meta.items():
                    base_meta.setdefault(meta_key, meta_value)

        if "status" not in base_entry and "status" in new_entry:
            base_entry["status"] = new_entry["status"]

    def _apply_history_max_items(
        self,
        payload: Dict[str, Any],
        prompt_id: Optional[str],
        max_items: Optional[int],
    ) -> None:
        if max_items is None or max_items < 0:
            return

        if prompt_id is None:
            keys = list(payload.keys())
            for stale_key in keys[max_items:]:
                payload.pop(stale_key, None)
            return

        if not payload:
            return

        entry = next(iter(payload.values()))
        outputs = entry.get("outputs")
        if isinstance(outputs, list):
            entry["outputs"] = outputs[:max_items]
        elif isinstance(outputs, dict):
            output_keys = list(outputs.keys())
            for stale_key in output_keys[max_items:]:
                outputs.pop(stale_key, None)

    @staticmethod
    def _history_provider_display_name(
        provider: Callable[[Optional[str], Optional[int]], Awaitable[Optional[Dict[str, Any]]]]
    ) -> str:
        return getattr(provider, "__name__", provider.__class__.__name__)

    def add_on_prompt_handler(self, handler):
        self.on_prompt_handlers.append(handler)

    def trigger_on_prompt(self, json_data, *, fail_closed=False):
        for handler in self.on_prompt_handlers:
            try:
                json_data = handler(json_data)
            except Exception:
                if fail_closed:
                    raise
                logging.warning("[ERROR] An error occurred during the on_prompt_handler processing")
                logging.warning(traceback.format_exc())

        return json_data

    def send_progress_text(
        self, text: Union[bytes, bytearray, str], node_id: str, sid=None
    ):
        if isinstance(text, str):
            text = text.encode("utf-8")
        node_id_bytes = str(node_id).encode("utf-8")

        # Pack the node_id length as a 4-byte unsigned integer, followed by the node_id bytes
        message = struct.pack(">I", len(node_id_bytes)) + node_id_bytes + text

        self.send_sync(BinaryEventTypes.TEXT, message, sid)
