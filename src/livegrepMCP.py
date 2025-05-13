#!/usr/bin/env python3
"""
LiveGrepMCP - A minimal FastMCP client for LiveGrep search API
"""
import os
import sys
from typing import Dict, List, Literal, Optional, Any
from dotenv import load_dotenv
import httpx
import urllib.parse

from fastmcp import FastMCP

# Load environment variables from .env file
load_dotenv()

LIVEGREP_API_URL = os.getenv("LIVEGREP_API_URL")
if not LIVEGREP_API_URL:
    print("Error: LIVEGREP_API_URL environment variable not set")
    sys.exit(1)

LIVEGREP_API_URL = LIVEGREP_API_URL.rstrip("/")

# Initialize FastMCP server
mcp = FastMCP(name="LiveGrepMCP 🔍")


@mcp.tool()
def search_livegrep(
    query: str,
    fold_case: Literal["auto", "match", "ignore"] = "auto",
    regex: Literal["true", "false"] = "true",
    context: Literal["true", "false"] = "true",
    max_matches: Optional[int] = None
) -> Dict[str, Any]:
    """
    Search through LiveGrep API

    Parameters:
    - query (str): Search query - can include modifiers like:
        - path:XYZ (include only paths containing XYZ)
        - -path:XYZ (exclude paths containing XYZ)
        - repo:XYZ (include only repos containing XYZ)
        - -repo:XYZ (exclude repos containing XYZ)
    - fold_case (str): Case sensitivity ("auto", "match", or "ignore")
    - regex (str): Enable regex search ("true" or "false")
    - context (str): Include context in results ("true" or "false")
    - max_matches (int, optional): Limit the number of matches returned

    Returns:
    - dict: API response with search results
    """
    endpoint = f"{LIVEGREP_API_URL}/api/v1/search/"

    # If max_matches is provided, add it to the query string
    if max_matches is not None:
        query = f"{query} max_matches:{max_matches}"

    # Create the payload
    payload = {
        "q": query,
        "fold_case": fold_case,
        "regex": regex,
        "context": context
    }

    # URL encode the payload
    payload_str = urllib.parse.urlencode(payload)

    try:
        with httpx.Client() as client:
            response = client.post(
                endpoint,
                data=payload_str,
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as e:
        return {"error": f"Error making request: {e}", "status": "error"}


@mcp.resource("livegrep://search-help")
def get_search_help() -> Dict[str, str]:
    """
    Get help information for LiveGrep search syntax
    """
    return {
        "description": "LiveGrep Search Syntax Help",
        "basic_search": "Simple text or regex search",
        "path_filter": "path:XYZ (include only paths containing XYZ)",
        "path_exclude": "-path:XYZ (exclude paths containing XYZ)",
        "repo_filter": "repo:XYZ (include only repos containing XYZ)",
        "repo_exclude": "-repo:XYZ (exclude repos containing XYZ)",
        "max_matches": "max_matches:XYZ (limit the number of matches returned)",
        "fold_case_options": ["auto", "match", "ignore"],
        "regex_options": ["true", "false"],
        "context_options": ["true", "false"]
    }


@mcp.resource("livegrep://api-info")
def get_api_info() -> Dict[str, Any]:
    """
    Get information about the configured LiveGrep API
    """
    return {
        "api_url": LIVEGREP_API_URL,
        "endpoint": f"{LIVEGREP_API_URL}/v1/search/",
        "method": "POST",
        "content_type": "application/x-www-form-urlencoded"
    }


if __name__ == "__main__":
    mcp.run()
