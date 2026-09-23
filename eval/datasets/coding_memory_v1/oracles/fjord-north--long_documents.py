"""Immutable oracle for fjord-north:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The settlement path retains 34 days and uses UTC.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
