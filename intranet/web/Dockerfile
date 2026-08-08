# nginx serving the static ZCode mirror.
FROM nginx:1.27-alpine

# Drop the default site; use our config that also proxies /api -> ai.
RUN rm /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/nginx.conf

# /data is the shared volume: site/ + content/ + releases/ land here.
VOLUME ["/data"]

EXPOSE 80
