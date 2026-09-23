"""Immutable oracle for juniper-green:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The scheduler path retains 52 days and uses UTC.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
