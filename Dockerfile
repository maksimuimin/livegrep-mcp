FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/
COPY .env* ./

# Make the script executable
RUN chmod +x ./src/livegrepMCP.py

EXPOSE 8000

# Set the entrypoint to the FastMCP server
ENTRYPOINT ["fastmcp", "run", "src/livegrepMCP.py", "--transport", "stdio"]

# Default command - runs the FastMCP server
CMD []
