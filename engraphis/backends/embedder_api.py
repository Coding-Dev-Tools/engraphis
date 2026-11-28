"""API-based embedder — calls an OpenAI-compatible ``/v1/embeddings`` endpoint.

Supports generic OpenAI-compatible endpoints (OpenRouter, self-hosted gateways)
as well as the opt-in **NVIDIA NIM** retrieval preset (``nvidia-nim``), including
asymmetric ``input_type`` (``"query"`` vs ``"passage"``), optional truncation, and
hosted (``https://integrate.api.nvidia.com/v1``) or self-hosted NIM base URLs.

Design notes:
- Implements the ``Embedder`` protocol (``engraphis.core.interfaces.Embedder``).
- Dimension is validated when configured, or detected from the first API response.
- Batch embedding sends multiple inputs in one API call, with per-item fallback.
- Provider credentials, endpoints, and raw responses are never written to logs.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
from numbers import Integral
from typing import Any, Literal, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit

import numpy as np

from engraphis.backends.embedder_deterministic import MAX_EMBEDDING_DIM

logger = logging.getLogger("engraphis.embedder_api")

# Default OpenRouter endpoint (legacy ApiEmbedder default)
_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_API_KEY_ENV = "ENGRAPHIS_LLM_API_KEY"

# Opt-in NVIDIA NIM embedding preset defaults
NVIDIA_NIM_PRESET = "nvidia-nim"
NVIDIA_NIM_DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_NIM_DEFAULT_MODEL = "nvidia/nv-embedqa-e5-v5"
NVIDIA_NIM_API_KEY_ENV = "NVIDIA_API_KEY"
NVIDIA_NIM_BASE_URL_ENV = "NVIDIA_NIM_BASE_URL"
EMBED_API_KEY_ENV = "ENGRAPHIS_EMBED_API_KEY"
EMBED_BASE_URL_ENV = "ENGRAPHIS_EMBED_BASE_URL"

_VALID_INPUT_TYPES = frozenset({"query", "passage"})
_VALID_TRUNCATE_MODES = frozenset({"NONE", "START", "END"})


def _embeddings_endpoint(base_url: str) -> str:
    """Return one OpenAI-compatible embeddings URL from a root or v1 base."""
    parsed = urlsplit(base_url.strip())
    stripped_path = parsed.path.strip("/")
    path = f"/{stripped_path}" if stripped_path else ""
    if path.endswith("/embeddings"):
        return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))
    while path.endswith("/v1"):
        path = path[:-3].rstrip("/")
    return urlunsplit(
        (parsed.scheme, parsed.netloc, path + "/v1/embeddings", parsed.query, "")
    )


def _embedding_space_endpoint(url: str) -> str:
    """Remove request credentials while retaining the provider endpoint identity."""
    parsed = urlsplit(url)
    netloc = parsed.netloc.rsplit("@", 1)[-1].lower()
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))


def _is_loopback_url(url: str) -> bool:
    """Return True when *url* targets a loopback host (localhost / 127.0.0.1 / ::1)."""
    try:
        hostname = urlsplit(url.strip()).hostname
    except ValueError:
        return False
    if not hostname:
        return False
    lowered = hostname.lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


def _validate_preset_base_url(value: str) -> str:
    """Validate an opt-in preset embedding base URL without reflecting it in errors."""
    raw = str(value or "")
    if not raw or raw != raw.strip() or any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 for char in raw
    ):
        raise ValueError("embedding base URL contains whitespace or control characters")
    try:
        parts = urlsplit(raw)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        raise ValueError("embedding base URL is invalid") from None
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not hostname:
        raise ValueError("embedding base URL must be an absolute http(s) URL")
    if scheme != "https" and not _is_loopback_url(raw):
        raise ValueError("embedding base URL must use HTTPS unless it targets loopback")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("embedding base URL has an invalid port")
    if parts.username is not None or parts.password is not None:
        raise ValueError("embedding base URL must not contain embedded credentials")
    if "\\" in parts.netloc or any(char.isspace() for char in parts.netloc):
        raise ValueError("embedding base URL contains an invalid host")
    if parts.query or parts.fragment:
        raise ValueError("embedding base URL must not contain a query string or fragment")
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


class ApiEmbedder:
    """Embedder that calls an OpenAI-compatible /v1/embeddings API.

    Parameters
    ----------
    model : str
        Model identifier, e.g. ``"nvidia/nv-embedqa-e5-v5"``.
    base_url : str, optional
        API base URL (default: OpenRouter).
    api_key : str, optional
        API key. Falls back to ``api_key_env`` (default: ``ENGRAPHIS_LLM_API_KEY``).
    dim : int, optional
        Known embedding dimension. If not provided, detected from first response.
    space_version : str, optional
        Provider/operator revision for the returned vector space. Persisted API
        embeddings fail closed when this is omitted because mutable providers cannot
        be fingerprinted from a model selector alone.
    api_key_env : str, optional
        Environment variable name checked when ``api_key`` is omitted.
    default_input_type : {"query", "passage"}, optional
        When set, sends ``input_type`` with every request (required by NVIDIA NIM
        asymmetric retrieval models) and advertises ``supports_input_type = True``.
    truncate : {"NONE", "START", "END"}, optional
        Optional provider-side truncation mode (e.g. ``"END"`` for NVIDIA NIM).
    encoding_format : str, optional
        Optional encoding format forwarded in the payload (e.g. ``"float"``).
    allow_unauthenticated_loopback : bool, optional
        Allow omitting the ``Authorization`` header when targeting a loopback URL
        (e.g. a local self-hosted NVIDIA NIM microservice).
    embedding_identity : str, optional
        Override the backend family identity recorded in vector-space fingerprints.
    """

    supports_semantic_search = True
    embedding_mode = "semantic"
    embedding_identity = "api_embeddings"

    def __init__(
        self,
        model: str,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        dim: Optional[int] = None,
        space_version: Optional[str] = None,
        *,
        api_key_env: Optional[str] = None,
        default_input_type: Optional[Literal["query", "passage"]] = None,
        truncate: Optional[Literal["NONE", "START", "END"]] = None,
        encoding_format: Optional[str] = None,
        allow_unauthenticated_loopback: bool = False,
        embedding_identity: Optional[str] = None,
    ) -> None:
        self.model = model
        self._base_url = (base_url or _DEFAULT_BASE_URL).strip()
        self._space_version = (space_version or "").strip()
        self._api_key_env = (api_key_env or _DEFAULT_API_KEY_ENV).strip()
        if api_key is not None:
            self._api_key = str(api_key).strip()
        else:
            self._api_key = os.environ.get(self._api_key_env, "").strip()
        if default_input_type is not None:
            normalized_input_type = str(default_input_type).strip().lower()
            if normalized_input_type not in _VALID_INPUT_TYPES:
                raise ValueError("default_input_type must be 'query' or 'passage'")
            self._default_input_type: Optional[str] = normalized_input_type
        else:
            self._default_input_type = None
        if truncate is not None:
            normalized_truncate = str(truncate).strip().upper()
            if normalized_truncate not in _VALID_TRUNCATE_MODES:
                raise ValueError("truncate must be one of: NONE, START, END")
            self._truncate: Optional[str] = normalized_truncate
        else:
            self._truncate = None
        self._encoding_format = (encoding_format or "").strip() or None
        self._allow_unauthenticated_loopback = bool(allow_unauthenticated_loopback)
        if embedding_identity is not None:
            cleaned_identity = str(embedding_identity).strip()
            if not cleaned_identity:
                raise ValueError("embedding_identity must be non-empty")
            self.embedding_identity = cleaned_identity
        if dim is not None:
            if isinstance(dim, bool) or not isinstance(dim, Integral):
                raise ValueError("embedding dimension must be a positive integer")
            dim = int(dim)
            if not 1 <= dim <= MAX_EMBEDDING_DIM:
                raise ValueError(
                    f"embedding dimension must be between 1 and {MAX_EMBEDDING_DIM}"
                )
        self._dim = dim
        self._embeddings_url = _embeddings_endpoint(self._base_url)
        # A custom endpoint can contain embedded credentials or signed query
        # parameters, while provider-controlled model identifiers are also untrusted
        # log input. Do not copy either into logs.
        # Do not log endpoint, model, dimensions, or any authentication-related state:
        # provider configuration can contain account identifiers or signed parameters.
        logger.info("API embedder initialized")

    @property
    def supports_input_type(self) -> bool:
        """True when the endpoint expects asymmetric query/passage ``input_type``."""
        return self._default_input_type is not None

    @property
    def is_remote(self) -> bool:
        """True when requests leave loopback to a remote embedding service."""
        return not _is_loopback_url(self._embeddings_url)

    @property
    def dim(self) -> int:
        if self._dim is None:
            # Probe the API to get dimension
            probe = self.embed(["hello"])
            self._dim = probe.shape[1]
        return self._dim  # type: ignore[return-value]

    @property
    def embedding_version(self) -> str:
        """Return a credential-free fingerprint, or empty for an unversioned API."""
        if not self._space_version:
            return ""
        payload = (
            f"v2\0{_embedding_space_endpoint(self._embeddings_url)}\0"
            f"{self.model}\0{self.dim}\0{self._space_version}"
        ).encode("utf-8")
        return "v2:" + hashlib.sha256(payload).hexdigest()

    def _resolve_input_type(
        self, input_type: Optional[Literal["query", "passage"]]
    ) -> Optional[str]:
        if input_type is None:
            return self._default_input_type
        normalized = str(input_type).strip().lower()
        if normalized not in _VALID_INPUT_TYPES:
            raise ValueError("input_type must be 'query' or 'passage'")
        return normalized

    def _build_headers(self) -> dict[str, str]:
        if self._api_key:
            return {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
        if self._allow_unauthenticated_loopback and not self.is_remote:
            return {"Content-Type": "application/json"}
        logger.error("No API key set for API embedder")
        raise RuntimeError(
            f"ApiEmbedder requires an API key via {self._api_key_env} "
            "env var or the api_key parameter"
        )

    def _build_payload(
        self, texts: list[str], *, input_type: Optional[str] = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "input": texts,
        }
        if input_type is not None:
            payload["input_type"] = input_type
        if self._encoding_format is not None:
            payload["encoding_format"] = self._encoding_format
        if self._truncate is not None:
            payload["truncate"] = self._truncate
        return payload

    def embed(
        self,
        texts: list[str],
        *,
        kind: Literal["text", "code"] = "text",
        input_type: Optional[Literal["query", "passage"]] = None,
    ) -> np.ndarray:
        """Embed a list of strings via the API.

        Uses ``/v1/embeddings`` with batch input.
        Falls back to per-item requests if the batch fails.

        Notes
        -----
        The ``kind`` parameter is accepted for protocol compatibility
        (``engraphis.core.interfaces.Embedder``) but is not used by the
        API embedder — the same endpoint handles both text and code.
        ``input_type`` (``"query"`` or ``"passage"``) is forwarded when supplied
        or when ``default_input_type`` is configured (for example NVIDIA NIM).
        """
        resolved_input_type = self._resolve_input_type(input_type)
        if not texts:
            # An empty batch must not probe a remote provider merely to discover a
            # dimension. Unknown is represented by the only truthful width: zero.
            return np.empty((0, self._dim or 0), dtype=np.float32)

        import httpx

        headers = self._build_headers()
        payload = self._build_payload(texts, input_type=resolved_input_type)

        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(
                    self._embeddings_url, headers=headers, json=payload
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            if len(texts) == 1:
                logger.warning(
                    "Embedding request failed (%s)",
                    type(exc).__name__,
                )
                return self._finalize_vectors([None], 1)
            logger.warning("Batch embedding request failed; falling back per-item")
            # Fallback: embed one at a time
            vecs: list[Optional[list[float]]] = [
                self._embed_one(t, input_type=resolved_input_type) for t in texts
            ]
            return self._finalize_vectors(vecs, len(texts))

        vectors = self._ordered_batch_vectors(data, len(texts))
        if vectors is None:
            if len(texts) == 1:
                logger.warning("API returned malformed embedding data")
                return self._finalize_vectors([None], 1)
            logger.warning("API returned malformed embedding data; falling back per-item")
            vecs = [
                self._embed_one(t, input_type=resolved_input_type) for t in texts
            ]
            return self._finalize_vectors(vecs, len(texts))
        return self._finalize_vectors(vectors, len(texts))

    def _finalize_vectors(
        self,
        vectors: Sequence[Optional[list[float]]],
        count: int,
    ) -> np.ndarray:
        """Assemble one finite, consistently-sized, L2-normalized vector per input."""
        if len(vectors) != count:
            raise RuntimeError("embedding provider returned an incomplete response")
        widths = {len(vector) for vector in vectors if vector is not None}
        if self._dim is not None:
            widths.add(self._dim)
        if not widths:
            # Without a configured or successfully observed width, zero vectors
            # cannot establish an embedding-space contract.  Guessing 384 here
            # would poison future successful responses from a differently-sized
            # provider model.
            raise RuntimeError("embedding provider returned no usable vectors")
        if any(vector is None for vector in vectors):
            raise RuntimeError("embedding provider returned an incomplete response")
        if len(widths) > 1:
            raise RuntimeError("embedding provider returned inconsistent dimensions")
        dimension = next(iter(widths))
        if not 1 <= dimension <= MAX_EMBEDDING_DIM:
            raise RuntimeError("embedding provider returned an invalid dimension")
        result = np.asarray(vectors, dtype=np.float32)
        if result.shape != (count, dimension) or not np.isfinite(result).all():
            raise RuntimeError("embedding provider returned malformed vectors")
        # Normalize in float64: a finite float32 vector near the representable
        # maximum can overflow a float32 norm to inf and collapse to all zeros.
        result64 = result.astype(np.float64)
        norms = np.linalg.norm(result64, axis=1, keepdims=True)
        if not np.isfinite(norms).all():
            raise RuntimeError("embedding provider returned an invalid vector norm")
        norms = np.where(norms == 0, 1.0, norms)
        result = (result64 / norms).astype(np.float32)
        if self._dim is None:
            self._dim = dimension
        return result

    def _coerce_vector(self, value) -> Optional[list[float]]:
        """Validate provider-controlled vector shape without reflecting its data."""
        try:
            vector = np.asarray(value, dtype=np.float32)
        except (TypeError, ValueError, OverflowError):
            return None
        if self._dim is not None and vector.ndim == 1 and vector.size != self._dim:
            # A configured dimension is a compatibility contract with the vector
            # store.  Treating a provider/model mismatch as a malformed row would
            # make the fallback substitute a zero vector, silently corrupting
            # retrieval instead of surfacing the configuration error.
            raise RuntimeError("embedding provider returned an unexpected dimension")
        if (
            vector.ndim != 1
            or not 1 <= vector.size <= MAX_EMBEDDING_DIM
            or not np.isfinite(vector).all()
        ):
            return None
        return vector.tolist()

    def _ordered_batch_vectors(self, data, count: int) -> Optional[list[list[float]]]:
        """Return exactly one validated vector per requested input, in input order."""
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            return None
        items = data["data"]
        if len(items) != count:
            return None
        ordered: list[Optional[list[float]]] = [None] * count
        for item in items:
            if not isinstance(item, dict):
                return None
            index = item.get("index")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < count
                or ordered[index] is not None
            ):
                return None
            vector = self._coerce_vector(item.get("embedding"))
            if vector is None:
                return None
            ordered[index] = vector
        if any(vector is None for vector in ordered):
            return None
        widths = {len(vector) for vector in ordered if vector is not None}
        if len(widths) != 1:
            return None
        return [vector for vector in ordered if vector is not None]

    def _embed_one(
        self, text: str, *, input_type: Optional[str] = None
    ) -> Optional[list[float]]:
        """Embed a single string via the API."""
        import httpx

        headers = self._build_headers()
        payload = self._build_payload([text], input_type=input_type)

        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(
                    self._embeddings_url, headers=headers, json=payload
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            logger.error("Single embedding request failed")
            return None

        ordered = self._ordered_batch_vectors(data, 1)
        if ordered is not None:
            return ordered[0]
        logger.warning("API returned malformed single-item embedding data")
        return None


def create_nvidia_nim_embedder(
    model: Optional[str] = None,
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    dim: Optional[int] = None,
    space_version: Optional[str] = None,
    default_input_type: Literal["query", "passage"] = "passage",
    truncate: Optional[Literal["NONE", "START", "END"]] = "END",
) -> ApiEmbedder:
    """Construct an ``ApiEmbedder`` configured with the opt-in NVIDIA NIM preset.

    Defaults to NVIDIA's hosted API Catalog (``https://integrate.api.nvidia.com/v1``)
    and ``nvidia/nv-embedqa-e5-v5``, or targets a self-hosted NIM container when
    ``base_url`` / ``ENGRAPHIS_EMBED_BASE_URL`` / ``NVIDIA_NIM_BASE_URL`` is set.
    Sends asymmetric ``input_type`` (``"query"`` vs ``"passage"``) automatically.
    """
    resolved_model = (model or "").strip() or NVIDIA_NIM_DEFAULT_MODEL
    raw_base_url = (
        (base_url.strip() if base_url is not None else "")
        or os.environ.get(EMBED_BASE_URL_ENV, "").strip()
        or os.environ.get(NVIDIA_NIM_BASE_URL_ENV, "").strip()
        or NVIDIA_NIM_DEFAULT_BASE_URL
    )
    validated_base_url = _validate_preset_base_url(raw_base_url)
    resolved_key = (
        str(api_key).strip()
        if api_key is not None
        else (
            os.environ.get(EMBED_API_KEY_ENV, "").strip()
            or os.environ.get(NVIDIA_NIM_API_KEY_ENV, "").strip()
        )
    )
    if not resolved_key and not _is_loopback_url(validated_base_url):
        raise RuntimeError(
            f"NVIDIA NIM embeddings require an API key via {NVIDIA_NIM_API_KEY_ENV} "
            f"or {EMBED_API_KEY_ENV} (or a local self-hosted NIM endpoint via "
            f"{EMBED_BASE_URL_ENV} / {NVIDIA_NIM_BASE_URL_ENV})"
        )
    return ApiEmbedder(
        model=resolved_model,
        base_url=validated_base_url,
        api_key=resolved_key,
        dim=dim,
        space_version=space_version,
        api_key_env=f"{NVIDIA_NIM_API_KEY_ENV} or {EMBED_API_KEY_ENV}",
        default_input_type=default_input_type,
        truncate=truncate,
        encoding_format="float",
        allow_unauthenticated_loopback=True,
        embedding_identity="nvidia_nim",
    )


def create_api_embedder(
    model: str,
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    dim: Optional[int] = None,
    space_version: Optional[str] = None,
) -> ApiEmbedder:
    """Construct a generic OpenAI-compatible ``ApiEmbedder`` from explicit config."""
    resolved_model = (model or "").strip()
    if not resolved_model:
        raise ValueError("API embedder requires a non-empty model identifier")
    raw_base_url = (
        (base_url.strip() if base_url is not None else "")
        or os.environ.get(EMBED_BASE_URL_ENV, "").strip()
        or _DEFAULT_BASE_URL
    )
    validated_base_url = _validate_preset_base_url(raw_base_url)
    resolved_key = (
        str(api_key).strip()
        if api_key is not None
        else (
            os.environ.get(EMBED_API_KEY_ENV, "").strip()
            or os.environ.get(_DEFAULT_API_KEY_ENV, "").strip()
        )
    )
    if not resolved_key and not _is_loopback_url(validated_base_url):
        raise RuntimeError(
            f"API embeddings require an API key via {EMBED_API_KEY_ENV} "
            f"or {_DEFAULT_API_KEY_ENV}"
        )
    return ApiEmbedder(
        model=resolved_model,
        base_url=validated_base_url,
        api_key=resolved_key,
        dim=dim,
        space_version=space_version,
        api_key_env=f"{EMBED_API_KEY_ENV} or {_DEFAULT_API_KEY_ENV}",
        allow_unauthenticated_loopback=True,
    )
