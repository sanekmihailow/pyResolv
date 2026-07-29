"""pyresolv — a set of composable stages for processing firewall logs.

The stages (collect/trim/merge/aggregate/resolve) are independent Unix filters
that read stdin/-i and write stdout/-o, connected by shell pipes. Sources and
resolvers are plugins registered by name.
"""

__all__ = ["__version__"]

__version__ = "2.2.0"
