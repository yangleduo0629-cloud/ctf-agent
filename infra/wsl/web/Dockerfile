FROM node:22.17.1-bookworm-slim

ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH

WORKDIR /app
RUN corepack enable && corepack prepare pnpm@10.13.1 --activate
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY index.html ./
COPY src ./src

EXPOSE 3000
CMD ["pnpm", "dev", "--host", "0.0.0.0", "--port", "3000", "--strictPort"]
