FROM alpine:3.20

RUN apk add --no-cache stunnel ca-certificates && update-ca-certificates

COPY stunnel.conf /etc/stunnel/stunnel.conf

EXPOSE 7443

CMD ["stunnel", "/etc/stunnel/stunnel.conf"]


