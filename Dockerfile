# adl-mcp-server — image is used two ways:
#   1) container-attached stdio deploy: `docker run -i --rm --network <adl_net> adl-mcp-server`
#      (register that as the command in your MCP client; see DEPLOY.md)
#   2) the integration smoke harness (docker-compose.test.yml)
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY guards.py adl_api.py mcp_server.py ./

# stdio MCP — must be attached to a client over stdin/stdout (-i).
CMD ["python", "mcp_server.py"]
