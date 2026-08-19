class ModelError(Exception):
	pass


class ModelProviderError(ModelError):
	"""Exception raised when a model provider returns an error."""

	def __init__(
		self,
		message: str,
		status_code: int = 502,
		model: str | None = None,
	):
		super().__init__(message)
		self.message = message
		self.status_code = status_code
		self.model = model


class ModelRateLimitError(ModelProviderError):
	"""Exception raised when a model provider returns a rate limit error."""

	def __init__(
		self,
		message: str,
		status_code: int = 429,
		model: str | None = None,
	):
		super().__init__(message, status_code, model)


class ModelAuthBlockedError(ModelProviderError):
	"""Raised when the LLM endpoint returns an HTML portal/gateway page instead of a
	JSON completion (e.g. a captive portal, VPN/campus-network gate, or WAF block).

	This is a fatal, non-retryable condition: retrying or switching to a fallback LLM
	cannot fix a network gateway that is intercepting the request, so the agent should
	stop immediately with a clear message rather than burning steps and tokens.
	"""

	def __init__(
		self,
		message: str,
		status_code: int = 403,
		model: str | None = None,
	):
		super().__init__(message, status_code, model)
