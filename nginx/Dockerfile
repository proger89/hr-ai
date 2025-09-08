FROM nginx:1.27-alpine

COPY conf.d /etc/nginx/conf.d
RUN rm -f /etc/nginx/conf.d/default.conf || true

EXPOSE 80


