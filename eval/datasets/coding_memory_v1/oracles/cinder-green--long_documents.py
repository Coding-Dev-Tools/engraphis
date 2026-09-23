"""Immutable oracle for cinder-green:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The release path retains 24 days and uses UTC.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
