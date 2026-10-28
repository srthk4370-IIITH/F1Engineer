"""Tests never call a paid API: the engineer uses the deterministic offline brain unless a test builds a provider itself."""
import os

os.environ["F1E_LLM"] = "mock"
