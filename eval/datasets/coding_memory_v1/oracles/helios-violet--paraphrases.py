"""Immutable oracle for helios-violet:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-helios-violet', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
