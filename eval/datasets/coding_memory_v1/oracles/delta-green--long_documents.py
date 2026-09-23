"""Immutable oracle for delta-green:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The index path retains 28 days and uses UTC.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
