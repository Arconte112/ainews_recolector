# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container at /app
COPY main.py .
COPY test2.py .
# Copy the .env file (consider using Docker secrets or environment variables for production)

# Create the directory for temporary images if it doesn't exist
# RUN mkdir -p temp_images
# Note: The script seems to handle directory creation, so explicitly creating it might be redundant
# Depending on permissions, it might be safer to create it and set permissions if needed.

# This service no longer exposes an HTTP API; it only runs the Telegram bot loop.

# Define environment variables (example, override with -e at runtime)
# ENV TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}

# Run main.py when the container launches
CMD ["python", "main.py"] 
