"""Immutable oracle for fjord-green:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The settlement path retains 36 days and uses UTC.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
