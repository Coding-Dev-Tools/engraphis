"""Immutable oracle for delta-north:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The index path retains 26 days and uses UTC.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
