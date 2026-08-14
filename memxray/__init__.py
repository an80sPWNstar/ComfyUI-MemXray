"""MemXray: Windows-only memory introspection for ComfyUI.

Package internals only. Nothing in here imports torch or comfy at import
time - that happens lazily inside comfy_probe, so importing this package is
always safe even before ComfyUI has finished booting.
"""
