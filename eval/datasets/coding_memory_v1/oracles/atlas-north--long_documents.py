"""Immutable oracle for atlas-north:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The vault path retains 14 days and uses UTC.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
