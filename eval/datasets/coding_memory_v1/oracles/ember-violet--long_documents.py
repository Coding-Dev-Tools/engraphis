"""Immutable oracle for ember-violet:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The archive path retains 33 days and uses America/New_York.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
