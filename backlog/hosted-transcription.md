# Hosted transcription providers (Groq et al)

The transcribe capability
ships whisper-local (default) plus an OpenAI-compatible whisper-api provider
(base_url-configurable; ffmpeg chunking removes upload limits, so any
compatible host works today). To investigate for a documented recommended
config: Groq whisper-large-v3-turbo, Fireworks, Deepgram (URL ingestion, no
upload), Cloudflare Workers AI whisper — compare price per audio hour,
accuracy vs local medium, rate limits.
