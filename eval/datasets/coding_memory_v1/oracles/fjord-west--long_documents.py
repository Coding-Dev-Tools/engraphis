"""Immutable oracle for fjord-west:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The settlement path retains 35 days and uses America/New_York.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
