"""Immutable oracle for ember-green:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The archive path retains 32 days and uses UTC.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
