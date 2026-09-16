"""Immutable oracle for juniper-north:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The scheduler path retains 50 days and uses UTC.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
