"""Immutable oracle for atlas-green:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The vault path retains 16 days and uses UTC.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
