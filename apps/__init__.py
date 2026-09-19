# -*- encoding: utf-8 -*-
"""
Copyright (c) 2019 - present AppSeed.us
"""

import truststore

# import Flask 
from flask import Flask

from .config import Config

# Use the operating system trust store for HTTPS calls. This includes organization
# certificates installed by IT on Windows, macOS, and Linux.
truststore.inject_into_ssl()

# Inject Flask magic
app = Flask(__name__)

# load Configuration
app.config.from_object( Config ) 

# Import routing to render the pages
from apps import views  # Register route decorators on the shared app.

__all__ = ("app", "views")
