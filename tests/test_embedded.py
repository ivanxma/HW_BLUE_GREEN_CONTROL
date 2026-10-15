from modules.embedded import release_packages, version


def test_official_release_parser_does_not_use_test_archives():
    html='''(mysql-26.7.0-linux-glibc2.28-x86_64-minimal.tar.xz)
        MD5: <code class="md5">123456789012345678901234567890ab</code>
        (mysql-test-26.7.0-linux-glibc2.28-x86_64.tar.xz)
        MD5: <code class="md5">ffffffffffffffffffffffffffffffff</code>'''
    result=release_packages(html)
    assert len(result)==1
    assert result[0]['version']=='26.7.0'
    assert result[0]['filename'].endswith('minimal.tar.xz')


def test_current_calendar_release_version_is_supported():
    assert version('mysqld Ver 26.7.0 for Linux on aarch64')=='26.7.0'
    assert version('MySQL Shell 26.7.1')=='26.7.1'
