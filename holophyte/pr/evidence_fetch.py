from holophyte.pr import mockup


def unfetched(section, base, cap_mb):
    if base is None:
        return {}
    failed = {}
    for link in mockup.media_links(section):
        if link.kind != "snapshot" or link.url in failed or not mockup._under(
                link.url, base):
            continue
        try:
            mockup._download(link, base, cap_mb)
        except mockup.Refused as why:
            failed[link.url] = str(why)
    return failed
