"""Test doubles for the hardware this app runs on: a fake Arduino, a fake MindVision camera and a fake upload server.

These live outside the production packages on purpose. camera/__init__.py imports mocks.fake_mindvision only when the
config selects `type = "mock_mindvision"`. The end-to-end run is `python -m mocks.e2e`.
"""
