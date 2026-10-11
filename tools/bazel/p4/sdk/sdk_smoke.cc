#include <cstdio>
extern "C" {
#include <sai.h>
}

int main()
{
    sai_api_version_t version = 0;
    const sai_status_t status = sai_query_api_version(&version);
    std::printf("DASH SAI status=%d api=%u\n", status, static_cast<unsigned>(version));
    return status == SAI_STATUS_SUCCESS && version == SAI_API_VERSION ? 0 : 1;
}
