FROM node:20-slim

# Install Python and venv support
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Create and activate a virtual environment; add it to PATH
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Gemini CLI globally via npm
RUN npm install -g @google/gemini-cli

# Create workspace directory with correct ownership (node user = UID 1000)
RUN mkdir -p /workspace && chown node:node /workspace
ENV WORKSPACE_DIR=/workspace

WORKDIR /home/node

# Copy Python dependencies and app
COPY --chown=node:node requirements.txt .
COPY --chown=node:node api.py .

# Install Python dependencies inside the venv
RUN pip install --no-cache-dir -r requirements.txt

# Use the built-in non-root node user (UID 1000, matches host mi user)
USER node

EXPOSE 8000

ENTRYPOINT ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
