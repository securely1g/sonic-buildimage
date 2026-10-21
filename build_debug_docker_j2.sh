#! /bin/bash

echo "
ARG BASE=$1
FROM \$BASE AS base

ARG docker_container_name

## Make apt-get non-interactive
ENV DEBIAN_FRONTEND=noninteractive

{% if $2 is defined %}
{% if $2|length %}

COPY \
{% for deb in $2.split(' ') -%}
debs/{{ deb }}{{' '}}
{%- endfor -%}
debs/

RUN apt-get update

RUN dpkg -i \
{% for deb in $2.split(' ') -%}
debs/{{ deb }}{{' '}}
{%- endfor %} || apt-get -y install -f --no-remove

{% endif %}
{% endif %}

{% if $3 is defined %}
{% if $3|length %}

RUN apt install -f -y \
{% for dbg in $3.split(' ') -%}
{{ dbg }}{{' '}}
{%- endfor %}

{% endif %}
{% endif %}

"

# Optional source-built debug archives. Existing three-argument callers keep
# their original Debian-package behavior.
if [ -n "${4:-}" ]; then
    cat <<EOF
{% if $4 is defined %}
{% for archive in $4.split() %}
ADD files/{{ archive }} /
{% endfor %}
{% endif %}
EOF
fi

echo "

## Clean up
RUN apt-get clean -y; apt-get autoclean -y; apt-get autoremove -y
RUN rm -rf /debs

FROM \$BASE

RUN --mount=type=bind,from=base,target=/changes-to-image rsync -axAX --no-D --exclude=/sys --exclude=/proc --exclude=/dev --exclude=resolv.conf /changes-to-image/ /

"
