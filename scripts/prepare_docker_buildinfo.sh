#!/bin/bash

grep "^# SKIP_HOOK" $2 && exit 0

[[ ! -z "${DBGOPT}" && $0 =~ ${DBGOPT} ]] && set -x

BUILDINFO_BASE=/usr/local/share/buildinfo

SCRIPT_SRC_PATH=src/sonic-build-hooks
if [ -e ${SCRIPT_SRC_PATH} ]; then
	. ${SCRIPT_SRC_PATH}/scripts/utils.sh
else
	. ${BUILDINFO_BASE}/scripts/utils.sh
fi

IMAGENAME=$1
DOCKERFILE=$2
ARCH=$3
DOCKERFILE_TARGET=$4
DISTRO=$5


[ -z "$BUILD_SLAVE" ] && BUILD_SLAVE=n
[ -z "$DOCKERFILE_TARGET" ] && DOCKERFILE_TARGET=$DOCKERFILE
DOCKERFILE_PATH=$(dirname "$DOCKERFILE_TARGET")
BUILDINFO_PATH="${DOCKERFILE_PATH}/buildinfo"
BUILDINFO_VERSION_PATH="${BUILDINFO_PATH}/versions"
DOCKER_PATH=$(dirname $DOCKERFILE)

[ -d $BUILDINFO_PATH ] && rm -rf $BUILDINFO_PATH
mkdir -p $BUILDINFO_VERSION_PATH

# Get the debian distribution from the docker base image
if [ -z "$DISTRO" ]; then
    DOCKER_BASE_IMAGE=$(grep "^ARG BASE=" $DOCKERFILE | head -n 1 | awk '{print $2}' | cut -d'=' -f 2)
    if [ -z "$DOCKER_BASE_IMAGE" ]; then
        DOCKER_BASE_IMAGE=$(grep "^FROM" $DOCKERFILE | head -n 1 | awk '{print $2}')
    fi
    DISTRO=$(docker run --rm --entrypoint "" $DOCKER_BASE_IMAGE cat /etc/os-release | grep VERSION_CODENAME | cut -d= -f2)
    if [ -z "$DISTRO" ]; then
        DISTRO=$(docker run --rm --entrypoint "" $DOCKER_BASE_IMAGE cat /etc/apt/sources.list | grep deb.debian.org | awk '{print $3}')
        [ -z "$DISTRO" ] && DISTRO=jessie
    fi
fi

if [[ "$IMAGENAME" == sonic-slave-* ]] || [[ "$IMAGENAME" == docker-base-* ]] || [[ "$IMAGENAME" == docker-ptf ]]; then
    scripts/build_mirror_config.sh ${DOCKERFILE_PATH} $ARCH $DISTRO
	mkdir -p "${DOCKERFILE_PATH}/files/apt/apt.conf.d"
	cp -f files/apt/apt.conf.d/* "${DOCKERFILE_PATH}/files/apt/apt.conf.d/"
fi

# add script for reproducible build. using sha256 instead of tag for docker base image.
scripts/docker_version_control.sh $@

# Copy shared build info first. Local execution trust must never be written to
# src/sonic-build-hooks: that source tree is also installed into runtime images.
cp -rf src/sonic-build-hooks/buildinfo/* "$BUILDINFO_PATH"

# Stage the shared cargo-auditable wrapper for the native execution image.
if [[ "$IMAGENAME" == sonic-slave-* ]] && [ -f files/build/cargo-wrapper ]; then
    cp files/build/cargo-wrapper "$BUILDINFO_PATH/cargo-wrapper"
    chmod 0755 "$BUILDINFO_PATH/cargo-wrapper"
fi

# Remove our old generated block on every preparation, including when trust is
# disabled. Dockerfile content participates in both native slave image tags.
if [ -f "$DOCKERFILE_TARGET" ]; then
    TRUST_CLEAN_FILE=$(mktemp)
    awk '
        /^# SONIC native execution trust BEGIN$/ { if (inside) exit 1; inside=1; next }
        /^# SONIC native execution trust END$/ { if (!inside) exit 1; inside=0; next }
        !inside { print }
        END { if (inside) exit 1 }
    ' "$DOCKERFILE_TARGET" > "$TRUST_CLEAN_FILE" || { rm -f "$TRUST_CLEAN_FILE"; exit 1; }
    cat "$TRUST_CLEAN_FILE" > "$DOCKERFILE_TARGET"
    rm -f "$TRUST_CLEAN_FILE"
fi

SLAVE_TRUST_FILE="$BUILDINFO_PATH/sonic-build-ca-bundle.pem"
SLAVE_TRUST_CERTIFICATES="$BUILDINFO_PATH/sonic-build-ca-certificates"
rm -f -- "$SLAVE_TRUST_FILE"
rm -rf -- "$SLAVE_TRUST_CERTIFICATES"
SLAVE_TRUST_SHA256=
if [[ "$BUILD_SLAVE" == y && "$IMAGENAME" == sonic-slave-* && -n "${SONIC_BUILD_SLAVE_CA_BUNDLE:-}" ]]; then
    python3 tools/bazel/ci/trust.py stage --source "$SONIC_BUILD_SLAVE_CA_BUNDLE" \
        --output "$SLAVE_TRUST_FILE" --certificates-output "$SLAVE_TRUST_CERTIFICATES" > /dev/null || exit 1
    SLAVE_TRUST_SHA256=$(sha256sum "$SLAVE_TRUST_FILE" | cut -d' ' -f1)
fi

DOCKERFILE_PRE_SCRIPT='# Auto-Generated for buildinfo
ARG SONIC_VERSION_CACHE
ARG SONIC_VERSION_CONTROL_COMPONENTS
ARG ENABLE_SBOM=n
COPY ["buildinfo", "/usr/local/share/buildinfo"]
COPY vcache/ /sonic/target/vcache/'${IMAGENAME}'
RUN dpkg -i /usr/local/share/buildinfo/sonic-build-hooks_1.0_all.deb
ENV IMAGENAME='${IMAGENAME}'
ENV DISTRO='${DISTRO}'
ENV ENABLE_SBOM=$ENABLE_SBOM
RUN pre_run_buildinfo '${IMAGENAME}'
'

DOCKERFILE_POST_SCRIPT='
RUN post_run_buildinfo '${IMAGENAME}'
RUN post_run_cleanup '${IMAGENAME}'
'

# Add the auto-generate code if it is not added in the target Dockerfile
if [ ! -f $DOCKERFILE_TARGET ] || ! grep -q "Auto-Generated for buildinfo" $DOCKERFILE_TARGET; then
    # Insert the docker build script before the RUN command
    LINE_NUMBER=$(grep -Fn -m 1 'RUN' $DOCKERFILE | cut -d: -f1)
    COPY_BASE_LINE_NUMBER=$(grep -n -m 1 'FROM \$BASE$' $DOCKERFILE | cut -d: -f1)
    if [ -z "$COPY_BASE_LINE_NUMBER" ]; then
        COPY_BASE_LINE_NUMBER=$(grep -n -m 1 'FROM scratch$' $DOCKERFILE | cut -d: -f1)
    fi
    TEMP_FILE=$(mktemp)
    if [ -n "$COPY_BASE_LINE_NUMBER" ]; then
        awk -v prescript="${DOCKERFILE_PRE_SCRIPT}" -v linenumber=$LINE_NUMBER -v postscript="${DOCKERFILE_POST_SCRIPT}" -v copybaselinenumber=$COPY_BASE_LINE_NUMBER 'NR==copybaselinenumber{print postscript} NR==linenumber{print prescript}1' $DOCKERFILE > $TEMP_FILE
    else
        awk -v prescript="${DOCKERFILE_PRE_SCRIPT}" -v linenumber=$LINE_NUMBER 'NR==linenumber{print prescript}1' $DOCKERFILE > $TEMP_FILE

        # Append the docker build script at the end of the docker file
        echo -e "\nRUN post_run_buildinfo ${IMAGENAME} " >> $TEMP_FILE
        echo -e "\nRUN post_run_cleanup ${IMAGENAME} " >> $TEMP_FILE
    fi

    cat $TEMP_FILE > $DOCKERFILE_TARGET
    rm -f $TEMP_FILE
fi

# Native execution trust is injected before the first RUN; ca-certificates is
# not installed yet. Client configuration uses the validated bundle directly.
# The package's later postinst also picks up the individual CA files for clients
# run through sudo, which may discard the client environment overrides.
# This opt-in does not change inherited recipes' existing TLS options.
if [ -n "$SLAVE_TRUST_SHA256" ]; then
    TRUST_BLOCK_FILE=$(mktemp)
    TRUST_DOCKERFILE=$(mktemp)
    cat > "$TRUST_BLOCK_FILE" <<EOF
# SONIC native execution trust BEGIN
# Execution CA bundle SHA256: $SLAVE_TRUST_SHA256
COPY ["buildinfo/sonic-build-ca-bundle.pem", "/usr/local/share/sonic-build-trust/ca-bundle.pem"]
COPY ["buildinfo/sonic-build-ca-certificates/", "/usr/local/share/ca-certificates/sonic-build-trust/"]
ENV SSL_CERT_FILE=/usr/local/share/sonic-build-trust/ca-bundle.pem
ENV GIT_SSL_CAINFO=/usr/local/share/sonic-build-trust/ca-bundle.pem
ENV CURL_CA_BUNDLE=/usr/local/share/sonic-build-trust/ca-bundle.pem
ENV REQUESTS_CA_BUNDLE=/usr/local/share/sonic-build-trust/ca-bundle.pem
ENV PIP_CERT=/usr/local/share/sonic-build-trust/ca-bundle.pem
ENV WGETRC=/usr/local/share/sonic-build-trust/wgetrc
RUN mkdir -p /etc/apt/apt.conf.d && \\
    printf '%s\\n' 'Acquire::https::CaInfo "/usr/local/share/sonic-build-trust/ca-bundle.pem";' > /etc/apt/apt.conf.d/99sonic-native-build-ca && \\
    printf '%s\\n' '[http]' '    sslCAInfo = /usr/local/share/sonic-build-trust/ca-bundle.pem' >> /etc/gitconfig && \\
    printf '%s\\n' 'ca_certificate = /usr/local/share/sonic-build-trust/ca-bundle.pem' 'check_certificate = on' > /usr/local/share/sonic-build-trust/wgetrc
# SONIC native execution trust END
EOF
    awk -v trustfile="$TRUST_BLOCK_FILE" '
        /^RUN dpkg -i \/usr\/local\/share\/buildinfo\/sonic-build-hooks_1\.0_all\.deb$/ {
            while ((getline line < trustfile) > 0) print line
            close(trustfile)
            inserted++
        }
        { print }
        END { if (inserted != 1) exit 1 }
    ' "$DOCKERFILE_TARGET" > "$TRUST_DOCKERFILE" || {
        rm -f "$TRUST_BLOCK_FILE" "$TRUST_DOCKERFILE"
        exit 1
    }
    cat "$TRUST_DOCKERFILE" > "$DOCKERFILE_TARGET"
    rm -f "$TRUST_BLOCK_FILE" "$TRUST_DOCKERFILE"
fi

# Generate the version lock files
scripts/versions_manager.py generate -t "$BUILDINFO_VERSION_PATH" -n "$IMAGENAME" -d "$DISTRO" -a "$ARCH"

touch $BUILDINFO_VERSION_PATH/versions-deb

# Create the cache directories
LOCAL_CACHE_DIR=target/vcache/${IMAGENAME}
mkdir -p ${LOCAL_CACHE_DIR} ${DOCKER_PATH}/vcache/
chmod -f 777 ${LOCAL_CACHE_DIR} ${DOCKER_PATH}/vcache/

if [[ "$SKIP_BUILD_HOOK" == y || ${ENABLE_VERSION_CONTROL_DOCKER} != y ]]; then
	exit 0
fi

# Version cache
DOCKER_IMAGE_NAME=${IMAGENAME}
IMAGE_DBGS_NAME=${DOCKER_IMAGE_NAME//-/_}_image_dbgs

if [[ ${DOCKER_IMAGE_NAME} == sonic-slave-* ]]; then
	GLOBAL_CACHE_DIR=${SONIC_VERSION_CACHE_SOURCE}/${DOCKER_IMAGE_NAME}
else
	GLOBAL_CACHE_DIR=/vcache/${DOCKER_IMAGE_NAME}
fi

SRC_VERSION_PATH=files/build/versions
if [ ! -z ${SONIC_VERSION_CACHE} ]; then

	# Version files for SHA calculation
	VERSION_FILES="${SRC_VERSION_PATH}/dockers/${DOCKER_IMAGE_NAME}/versions-*-${DISTRO}-${ARCH} ${SRC_VERSION_PATH}/default/versions-*"
	DEP_FILES="Dockerfile.j2"
	if [[ ${DOCKER_IMAGE_NAME} =~ '-dbg' ]]; then
		DEP_DBG_FILES="build_debug_docker_j2.sh"
	fi

	#Calculate the version SHA
	VERSION_SHA="$( (echo -n "${!IMAGE_DBGS_NAME}"; \
		(cd ${DOCKER_PATH}; cat ${DEP_FILES}); \
		cat ${DEP_DBG_FILES} ${VERSION_FILES}) \
		| sha1sum | awk '{print substr($1,0,23);}')"

	GLOBAL_CACHE_FILE=${GLOBAL_CACHE_DIR}/${DOCKER_IMAGE_NAME}-${VERSION_SHA}.tgz
	LOCAL_CACHE_FILE=${LOCAL_CACHE_DIR}/cache.tgz
	GIT_FILE_STATUS=$(git status -s ${DEP_FILES})

	# Create the empty cache tar file as local cache
	if [[ ! -f ${LOCAL_CACHE_FILE} ]]; then
		tar -zcf ${LOCAL_CACHE_FILE} -T /dev/null
		chmod -f 777 ${LOCAL_CACHE_FILE}
	fi

	# Global cache file exists, load from global cache.
	if [[  -e ${GLOBAL_CACHE_FILE} ]]; then
		cp ${GLOBAL_CACHE_FILE} ${LOCAL_CACHE_FILE}
		touch ${GLOBAL_CACHE_FILE}
	else
		# When file is modified, Global SHA is calculated with the local change.
		# Load from the previous version of build cache if exists
		VERSIONS=( "HEAD" "HEAD~1" "HEAD~2" )
		for VERSION in ${VERSIONS[@]}; do
			VERSION_PREV_SHA="$( (echo -n "${!IMAGE_DBGS_NAME}"; \
				(cd ${DOCKER_PATH}; git --no-pager show $(ls -f ${DEP_FILES}|sed 's|.*|'${VERSION}':./&|g')); \
				(git --no-pager show $(ls -f ${DEP_DBG_FILES} ${VERSION_FILES}|sed 's|.*|'${VERSION}':&|g'))) \
				| sha1sum | awk '{print substr($1,0,23);}')"
			GLOBAL_PREV_CACHE_FILE=${GLOBAL_CACHE_DIR}/${DOCKER_IMAGE_NAME}-${VERSION_PREV_SHA}.tgz
			if [[  -e ${GLOBAL_PREV_CACHE_FILE} ]]; then
				cp ${GLOBAL_PREV_CACHE_FILE} ${LOCAL_CACHE_FILE}
				touch ${GLOBAL_PREV_CACHE_FILE}
				break
			fi
		done
	fi

	rm -f ${DOCKER_PATH}/vcache/cache.tgz
	ln -f ${LOCAL_CACHE_FILE} ${DOCKER_PATH}/vcache/cache.tgz


else
	# Delete the cache file if version cache is disabled.
	rm -f ${DOCKER_PATH}/vcache/cache.tgz
fi
