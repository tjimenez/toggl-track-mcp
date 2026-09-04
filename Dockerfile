FROM python:3.12-slim

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the server
COPY server.py toggl_focus.py ./

# Run the server
CMD ["python", "server.py"]
